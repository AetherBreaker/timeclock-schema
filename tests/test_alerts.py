# Standard library imports
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4
from zoneinfo import ZoneInfo

# Third party imports
import pytest

# First party imports
from timeclock_schema.alerts import (
  ALERT_TYPES,
  CONFIRMATION_NOTICE,
  HOOKS,
  WEEKLY_HOURS,
  AlertContext,
  Firing,
  Hook,
  ThresholdType,
  fired_hooks,
  register_hook,
  register_type,
)
from timeclock_schema.enums import PunchKind
from timeclock_schema.models import Punch, Settings

NY = ZoneInfo("America/New_York")
MONDAY = datetime(2026, 10, 26, 8, 0, tzinfo=NY)
H = timedelta(hours=1)


def punch(kind: PunchKind, at: datetime) -> Punch:
  return Punch(id=uuid4(), employee_id=1, kind=kind, at=at, instance_id="1")


def week_of_shifts(days: int, hours: timedelta, *, break_minutes: int = 0) -> list[Punch]:
  """`days` shifts of `hours` from Monday, each with one break of `break_minutes` (counted within `hours`)."""
  punches: list[Punch] = []
  for day in range(days):
    start = MONDAY + day * 24 * H
    punches.append(punch(PunchKind.CLOCK_IN, start))
    if break_minutes:
      punches += [punch(PunchKind.BREAK_START, start + H), punch(PunchKind.BREAK_END, start + H + timedelta(minutes=break_minutes))]
    punches.append(punch(PunchKind.CLOCK_OUT, start + hours))
  return punches


def context(punches: list[Punch], *, settings: Settings | None = None, now: datetime = MONDAY + 6 * 24 * H) -> AlertContext:
  return AlertContext(employee_id=1, punches=punches, corrections=(), settings=settings or Settings(), tz=NY, now=now)


def test_version_one_registers_the_weekly_hours_notice():
  assert set(ALERT_TYPES) == {WEEKLY_HOURS}
  assert HOOKS == [Hook(WEEKLY_HOURS, timedelta(), (CONFIRMATION_NOTICE,))]


def test_weekly_hours_counts_worked_plus_paid_breaks():
  # Five 8-hour shifts, each with a paid 30-minute break: 37.5 h worked + 2.5 h paid = 40 h, exactly the default limit.
  ctx = context(week_of_shifts(5, 8 * H, break_minutes=30))
  assert ALERT_TYPES[WEEKLY_HOURS].measure(ctx) == 40 * H
  assert fired_hooks(ctx) == (Firing(HOOKS[0], measure=40 * H, limit=40 * H),)


def test_weekly_hours_hook_waits_for_the_limit():
  assert fired_hooks(context(week_of_shifts(5, 8 * H - timedelta(minutes=1)))) == ()
  assert fired_hooks(context(week_of_shifts(5, 8 * H), settings=Settings(weekly_hours_alert=Decimal("40.01")))) == ()


def test_a_lead_time_fires_early():
  early = Hook(WEEKLY_HOURS, H, (CONFIRMATION_NOTICE,))
  ctx = context(week_of_shifts(5, 8 * H - timedelta(minutes=12)))  # 39 h
  assert fired_hooks(ctx, hooks=[*HOOKS, early]) == (Firing(early, measure=39 * H, limit=40 * H),)


def test_the_limit_follows_the_setting():
  ctx = context(week_of_shifts(4, 8 * H), settings=Settings(weekly_hours_alert=Decimal("32.25")))
  assert fired_hooks(ctx) == ()  # 32 h worked, under 32.25
  more = context(week_of_shifts(4, 8 * H + timedelta(minutes=4)), settings=ctx.settings)  # 32 h 16 min
  assert fired_hooks(more) == (Firing(HOOKS[0], measure=32 * H + timedelta(minutes=16), limit=32 * H + timedelta(minutes=15)),)
  assert ALERT_TYPES[WEEKLY_HOURS].limit(ctx.settings) == 32 * H + timedelta(minutes=15)


def test_only_this_week_counts():
  last_week = [punch(PunchKind.CLOCK_IN, MONDAY - 7 * 24 * H), punch(PunchKind.CLOCK_OUT, MONDAY - 7 * 24 * H + 50 * H)]
  assert ALERT_TYPES[WEEKLY_HOURS].measure(context(last_week)) == timedelta()


def test_registration_refuses_duplicates_and_unknown_types():
  with pytest.raises(ValueError, match="already registered"):
    register_type(ThresholdType(WEEKLY_HOURS, lambda _ctx: timedelta(), lambda _s: H))
  with pytest.raises(ValueError, match="unknown alert type"):
    register_hook(Hook("nope", timedelta(), (CONFIRMATION_NOTICE,)))
  with pytest.raises(ValueError, match="lead"):
    register_hook(Hook(WEEKLY_HOURS, -H, (CONFIRMATION_NOTICE,)))


def test_context_rejects_a_naive_now():
  with pytest.raises(ValueError, match="naive"):
    context([], now=datetime(2026, 10, 27, 12))  # noqa: DTZ001
  assert context([], now=datetime(2026, 10, 27, 12, tzinfo=UTC)).now.tzinfo is UTC
