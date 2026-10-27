"""Read-side shift rules (spec §4.5): the one implementation the clocks and the backend share, so payroll sees one answer."""

# Standard library imports
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Final
from uuid import UUID

# First party imports
from timeclock_schema.base import TimeclockModel, UtcDatetime
from timeclock_schema.enums import CorrectionAction, PunchKind

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Iterable, Mapping
  from zoneinfo import ZoneInfo

  # First party imports
  from timeclock_schema.models import Punch, PunchCorrection, Settings


class EmployeeState(StrEnum):
  """Where an employee is in the shift state machine."""

  OFF = "off"
  WORKING = "working"
  ON_BREAK = "on_break"
  AT_LUNCH = "at_lunch"


VALID_NEXT: Final[Mapping[EmployeeState, tuple[PunchKind, ...]]] = MappingProxyType(
  {
    EmployeeState.OFF: (PunchKind.CLOCK_IN,),
    EmployeeState.WORKING: (PunchKind.CLOCK_OUT, PunchKind.BREAK_START, PunchKind.LUNCH_START),
    EmployeeState.ON_BREAK: (PunchKind.BREAK_END, PunchKind.CLOCK_OUT),
    EmployeeState.AT_LUNCH: (PunchKind.LUNCH_END, PunchKind.CLOCK_OUT),
  }
)
"""The punches valid from each state, in the order the clock's menu shows them."""

# A valid punch's next state depends on its kind alone.
_NEXT_STATE: Final[Mapping[PunchKind, EmployeeState]] = MappingProxyType(
  {
    PunchKind.CLOCK_IN: EmployeeState.WORKING,
    PunchKind.CLOCK_OUT: EmployeeState.OFF,
    PunchKind.BREAK_START: EmployeeState.ON_BREAK,
    PunchKind.BREAK_END: EmployeeState.WORKING,
    PunchKind.LUNCH_START: EmployeeState.AT_LUNCH,
    PunchKind.LUNCH_END: EmployeeState.WORKING,
  }
)


class EffectivePunch(TimeclockModel):
  """A punch after its corrections."""

  id: UUID
  kind: PunchKind
  at: UtcDatetime
  automatic: bool


class Interval(TimeclockModel):
  """A break or lunch; `end` is None while it is still open."""

  start: UtcDatetime
  end: UtcDatetime | None


class Shift(TimeclockModel):
  """A clock-in, its clock-out if any, and the breaks and lunches between."""

  clock_in: EffectivePunch
  clock_out: EffectivePunch | None
  breaks: tuple[Interval, ...]
  lunches: tuple[Interval, ...]

  @property
  def is_open(self) -> bool:
    """Whether the shift has no clock-out yet."""
    return self.clock_out is None


class Anomaly(TimeclockModel):
  """A punch not valid from `state`, the state it arrived in; it changes neither state nor totals."""

  punch: EffectivePunch
  state: EmployeeState


class Timeline(TimeclockModel):
  """One employee's punches walked through the state machine."""

  state: EmployeeState
  shifts: tuple[Shift, ...]
  anomalies: tuple[Anomaly, ...]
  last_event_at: UtcDatetime | None
  """The last punch applied to state; anomalies don't count."""

  @property
  def open_shift(self) -> Shift | None:
    """The last shift, if it is still open."""
    return self.shifts[-1] if self.shifts and self.shifts[-1].is_open else None

  @property
  def valid_next(self) -> tuple[PunchKind, ...]:
    """The punches valid from the current state, in menu order."""
    return VALID_NEXT[self.state]


def apply_corrections(punches: Iterable[Punch], corrections: Iterable[PunchCorrection]) -> list[EffectivePunch]:
  """Apply each punch's newest correction and sort the survivors.

  Duplicate punch ids (a LAN copy and a synced copy) keep the first seen. Corrections for punches not passed in are ignored:
  a clock holds one week, but corrections to older punches still reach it.

  Returns:
    The effective punches, sorted by `(at, id)` so identical instants order deterministically.
  """
  unique: dict[UUID, Punch] = {}
  for p in punches:
    unique.setdefault(p.id, p)
  newest: dict[UUID, PunchCorrection] = {}
  for c in corrections:
    current = newest.get(c.punch_id)
    if c.punch_id in unique and (current is None or (c.recorded_at, c.id) > (current.recorded_at, current.id)):
      newest[c.punch_id] = c
  effective: list[EffectivePunch] = []
  for p in unique.values():
    c = newest.get(p.id)
    if c is None:
      effective.append(EffectivePunch(id=p.id, kind=p.kind, at=p.at, automatic=p.automatic))
    elif c.action is CorrectionAction.AMEND:
      at = p.at if c.new_at is None else c.new_at
      kind = p.kind if c.new_kind is None else c.new_kind
      effective.append(EffectivePunch(id=p.id, kind=kind, at=at, automatic=p.automatic))
  return sorted(effective, key=lambda e: (e.at, e.id))


def build_timeline(
  punches: Iterable[Punch], corrections: Iterable[PunchCorrection] = (), *, as_of: datetime | None = None
) -> Timeline:
  """Walk one employee's corrected punches through the state machine.

  A punch not valid from the current state is an anomaly, so of two consecutive same-type punches the oldest counts,
  automatic or not (R41.2). With `as_of`, a punch whose effective time is later is left out: the clock passes now, since
  the automatic clock-out may be stamped after it runs (§4.5), and a hand clock-out before that stamp then wins.

  Raises:
    ValueError: The punches belong to more than one employee.
  """
  punches = list(punches)
  if len({p.employee_id for p in punches}) > 1:
    raise ValueError("punches from more than one employee")
  events = [e for e in apply_corrections(punches, corrections) if as_of is None or e.at <= as_of]
  state = EmployeeState.OFF
  shifts: list[Shift] = []
  anomalies: list[Anomaly] = []
  last_event_at: datetime | None = None
  clock_in: EffectivePunch | None = None
  breaks: list[Interval] = []
  lunches: list[Interval] = []
  interval_start: datetime | None = None
  for p in events:
    if p.kind not in VALID_NEXT[state]:
      anomalies.append(Anomaly(punch=p, state=state))
      continue
    last_event_at = p.at
    # Every punch valid from a break or lunch ends it: its own end punch, or a clock-out ending it implicitly.
    if state in {EmployeeState.ON_BREAK, EmployeeState.AT_LUNCH}:
      assert interval_start is not None  # set by the start punch that entered this state
      (breaks if state is EmployeeState.ON_BREAK else lunches).append(Interval(start=interval_start, end=p.at))
    if p.kind is PunchKind.CLOCK_IN:
      clock_in, breaks, lunches = p, [], []
    elif p.kind in {PunchKind.BREAK_START, PunchKind.LUNCH_START}:
      interval_start = p.at
    elif p.kind is PunchKind.CLOCK_OUT:
      assert clock_in is not None  # clock_out is valid only after a clock_in
      shifts.append(Shift(clock_in=clock_in, clock_out=p, breaks=tuple(breaks), lunches=tuple(lunches)))
    state = _NEXT_STATE[p.kind]
  if state is not EmployeeState.OFF:
    assert clock_in is not None
    if state is not EmployeeState.WORKING:
      assert interval_start is not None
      (breaks if state is EmployeeState.ON_BREAK else lunches).append(Interval(start=interval_start, end=None))
    shifts.append(Shift(clock_in=clock_in, clock_out=None, breaks=tuple(breaks), lunches=tuple(lunches)))
  return Timeline(state=state, shifts=tuple(shifts), anomalies=tuple(anomalies), last_event_at=last_event_at)


class HoursTotals(TimeclockModel):
  """One employee's time in a range, as three separate totals (§4.5)."""

  worked: timedelta
  """Shift time less every lunch and break."""
  paid_break: timedelta
  """Break time within the daily allowance; not part of `worked`."""
  unpaid: timedelta
  """Lunch, plus break time over the allowance."""


def total_hours(
  punches: Iterable[Punch],
  corrections: Iterable[PunchCorrection] = (),
  *,
  settings: Settings,
  tz: ZoneInfo,
  start: datetime,
  end: datetime,
  now: datetime,
) -> HoursTotals:
  """Total one employee's time inside `[start, end)`: every rule for counting hours lives here, and only here (§4.5).

  - A punch whose effective time is after `now` doesn't count yet (the automatic clock-out may be written early, §8.4);
    an open shift, break or lunch runs until `now`.
  - Lunch is unpaid. Breaks are paid up to `settings.paid_break_minutes` per local calendar day in `tz`, each break
    charged in full to the day it started, earliest first; the rest is unpaid.
  - Each break is split where its day's allowance runs out, and the paid head and unpaid tail are then clipped to the
    range: a range edge cuts a break's parts, never changes how much of it is paid. So pass every punch of any day the
    range touches, or a day's earlier breaks go uncharged.

  Raises:
    ValueError: `start`, `end` or `now` is naive, or the punches belong to more than one employee.
  """
  if any(value.utcoffset() is None for value in (start, end, now)):
    raise ValueError("naive datetime; pass an aware one")
  start, end, now = start.astimezone(UTC), end.astimezone(UTC), now.astimezone(UTC)
  allowance = timedelta(minutes=settings.paid_break_minutes)
  allowance_left: dict[date, timedelta] = {}
  worked = paid_break = unpaid = timedelta()
  for shift in build_timeline(punches, corrections, as_of=now).shifts:  # chronological, so breaks are charged in order
    lo = max(shift.clock_in.at, start)
    hi = min(now if shift.clock_out is None else shift.clock_out.at, end)

    def counted(a: datetime, b: datetime, lo: datetime = lo, hi: datetime = hi) -> timedelta:
      # The part of [a, b) inside both the range and the shift's counted span; nothing when the shift lies outside.
      return max(min(b, hi) - max(a, lo), timedelta())

    worked += counted(lo, hi)
    for lunch in shift.lunches:
      lunch_time = counted(lunch.start, now if lunch.end is None else lunch.end)
      worked -= lunch_time
      unpaid += lunch_time
    # Charged even when the shift lies outside the range, so a later break that day sees the allowance already used.
    for brk in shift.breaks:
      brk_end = now if brk.end is None else brk.end
      day = brk.start.astimezone(tz).date()
      paid_length = min(allowance_left.get(day, allowance), brk_end - brk.start)
      allowance_left[day] = allowance_left.get(day, allowance) - paid_length
      paid_part, unpaid_part = counted(brk.start, brk.start + paid_length), counted(brk.start + paid_length, brk_end)
      worked -= paid_part + unpaid_part
      paid_break += paid_part
      unpaid += unpaid_part
  return HoursTotals(worked=worked, paid_break=paid_break, unpaid=unpaid)


def week_bounds(at: datetime, tz: ZoneInfo) -> tuple[datetime, datetime]:
  """Return the Monday 00:00 to next Monday 00:00 week in `tz` containing `at`, both in UTC (R33).

  Each bound is built from its own local midnight, so a week spanning a DST change is 167 or 169 hours long.

  Raises:
    ValueError: `at` is naive.
  """
  if at.utcoffset() is None:
    raise ValueError("naive datetime; pass an aware one")
  local = at.astimezone(tz)
  monday = local.date() - timedelta(days=local.weekday())
  week_start = datetime.combine(monday, time(0), tzinfo=tz)
  week_end = datetime.combine(monday + timedelta(days=7), time(0), tzinfo=tz)
  return week_start.astimezone(UTC), week_end.astimezone(UTC)


def auto_clock_out_due(timeline: Timeline, stamp: datetime) -> bool:
  """Whether the daily automatic clock-out at `stamp` should close this employee's shift (R41.1).

  Due when the employee isn't off and their last event precedes the stamp; a shift with a later event waits for the next
  day's run.
  """
  return timeline.state is not EmployeeState.OFF and timeline.last_event_at is not None and timeline.last_event_at < stamp
