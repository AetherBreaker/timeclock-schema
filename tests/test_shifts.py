# Standard library imports
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

# Third party imports
import pytest

# First party imports
from timeclock_schema.enums import CorrectionAction, PunchKind
from timeclock_schema.models import Punch, PunchCorrection, Settings
from timeclock_schema.shifts import (
  VALID_NEXT,
  EmployeeState,
  HoursTotals,
  Interval,
  auto_clock_out_due,
  build_timeline,
  total_hours,
  week_bounds,
)

CLOCK_IN, CLOCK_OUT = PunchKind.CLOCK_IN, PunchKind.CLOCK_OUT
BREAK_START, BREAK_END = PunchKind.BREAK_START, PunchKind.BREAK_END
LUNCH_START, LUNCH_END = PunchKind.LUNCH_START, PunchKind.LUNCH_END

T = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)  # Tuesday 08:00 EDT
H = timedelta(hours=1)
M = timedelta(minutes=1)
NY = ZoneInfo("America/New_York")
S = Settings()  # a 30-minute daily paid break allowance


def punch(kind: PunchKind, at: datetime, *, automatic: bool = False, punch_id: UUID | None = None, employee_id: int = 1) -> Punch:
  return Punch(id=punch_id or uuid4(), employee_id=employee_id, kind=kind, at=at, instance_id="1", automatic=automatic)


def test_valid_next_matches_spec_table():
  assert VALID_NEXT == {
    EmployeeState.OFF: (PunchKind.CLOCK_IN,),
    EmployeeState.WORKING: (PunchKind.CLOCK_OUT, PunchKind.BREAK_START, PunchKind.LUNCH_START),
    EmployeeState.ON_BREAK: (PunchKind.BREAK_END, PunchKind.CLOCK_OUT),
    EmployeeState.AT_LUNCH: (PunchKind.LUNCH_END, PunchKind.CLOCK_OUT),
  }


def test_valid_next_follows_the_state():
  assert build_timeline([punch(CLOCK_IN, T)]).valid_next == (CLOCK_OUT, BREAK_START, LUNCH_START)
  assert build_timeline([]).valid_next == (CLOCK_IN,)


def test_clock_in_then_out_closes_one_shift():
  tl = build_timeline([punch(CLOCK_IN, T), punch(CLOCK_OUT, T + 8 * H)])
  clock_out = tl.shifts[0].clock_out
  assert clock_out is not None
  assert tl.state is EmployeeState.OFF and len(tl.shifts) == 1 and clock_out.at == T + 8 * H
  assert tl.anomalies == () and tl.open_shift is None and tl.last_event_at == T + 8 * H


def test_break_and_lunch_intervals():
  tl = build_timeline(
    [
      punch(CLOCK_IN, T),
      punch(BREAK_START, T + 2 * H),
      punch(BREAK_END, T + 2 * H + 15 * M),
      punch(LUNCH_START, T + 4 * H),
      punch(LUNCH_END, T + 4 * H + 30 * M),
      punch(CLOCK_OUT, T + 8 * H),
    ]
  )
  assert tl.shifts[0].breaks == (Interval(start=T + 2 * H, end=T + 2 * H + 15 * M),)
  assert tl.shifts[0].lunches == (Interval(start=T + 4 * H, end=T + 4 * H + 30 * M),)


def test_clock_out_from_break_ends_the_break():
  tl = build_timeline([punch(CLOCK_IN, T), punch(BREAK_START, T + 3 * H), punch(CLOCK_OUT, T + 3 * H + 10 * M)])
  assert tl.state is EmployeeState.OFF and tl.shifts[0].breaks == (Interval(start=T + 3 * H, end=T + 3 * H + 10 * M),)


def test_invalid_punches_are_anomalies():
  stray_out, second_in = punch(CLOCK_OUT, T), punch(CLOCK_IN, T + 2 * H)
  tl = build_timeline([stray_out, punch(CLOCK_IN, T + H), second_in])
  assert [(a.punch.id, a.state) for a in tl.anomalies] == [
    (stray_out.id, EmployeeState.OFF),
    (second_in.id, EmployeeState.WORKING),
  ]
  assert tl.state is EmployeeState.WORKING and tl.last_event_at == T + H


def test_automatic_clock_out_stands_against_a_later_real_one():
  monday = T - 24 * H  # Monday 08:00 EDT
  auto_out, real_out = punch(CLOCK_OUT, monday + 15 * H, automatic=True), punch(CLOCK_OUT, T)
  punches = [punch(CLOCK_IN, monday), auto_out, real_out]
  tl = build_timeline(punches)
  assert len(tl.shifts) == 1 and tl.state is EmployeeState.OFF
  clock_out = tl.shifts[0].clock_out
  assert clock_out is not None and clock_out.id == auto_out.id
  assert total_hours(punches, settings=S, tz=NY, start=monday, end=T + H, now=T + H).worked == 15 * H
  assert [(a.punch.id, a.state) for a in tl.anomalies] == [(real_out.id, EmployeeState.OFF)]


def test_real_clock_out_stands_against_a_later_automatic_one():
  real_out, auto_out = punch(CLOCK_OUT, T + 9 * H), punch(CLOCK_OUT, T + 15 * H, automatic=True)
  tl = build_timeline([punch(CLOCK_IN, T), real_out, auto_out])
  clock_out = tl.shifts[0].clock_out
  assert clock_out is not None and clock_out.id == real_out.id
  assert [(a.punch.id, a.state) for a in tl.anomalies] == [(auto_out.id, EmployeeState.OFF)]


def test_oldest_of_two_consecutive_same_type_punches_counts():
  later_start = punch(BREAK_START, T + 2 * H + 5 * M)
  tl = build_timeline([punch(CLOCK_IN, T), punch(BREAK_START, T + 2 * H), later_start, punch(BREAK_END, T + 2 * H + 15 * M)])
  assert tl.shifts[0].breaks == (Interval(start=T + 2 * H, end=T + 2 * H + 15 * M),)
  assert [(a.punch.id, a.state) for a in tl.anomalies] == [(later_start.id, EmployeeState.ON_BREAK)]


def test_lone_automatic_clock_out_closes_the_shift():
  tl = build_timeline([punch(CLOCK_IN, T), punch(CLOCK_OUT, T + 10 * H, automatic=True)])
  clock_out = tl.shifts[0].clock_out
  assert clock_out is not None
  assert tl.state is EmployeeState.OFF and clock_out.automatic


def test_events_after_lone_automatic_clock_out_are_anomalies():
  late_break = punch(BREAK_START, T + 11 * H)
  tl = build_timeline([punch(CLOCK_IN, T), punch(CLOCK_OUT, T + 10 * H, automatic=True), late_break])
  assert tl.state is EmployeeState.OFF and [a.punch.id for a in tl.anomalies] == [late_break.id]


def test_void_then_newer_amend_restores_the_punch():
  out = punch(CLOCK_OUT, T + 8 * H)
  void = PunchCorrection(id=uuid4(), punch_id=out.id, action=CorrectionAction.VOID, recorded_at=T + 20 * H)
  assert build_timeline([punch(CLOCK_IN, T), out], [void]).state is EmployeeState.WORKING
  amend = PunchCorrection(id=uuid4(), punch_id=out.id, action=CorrectionAction.AMEND, new_at=T + 9 * H, recorded_at=T + 21 * H)
  tl = build_timeline([punch(CLOCK_IN, T), out], [void, amend])
  clock_out = tl.shifts[0].clock_out
  assert clock_out is not None
  assert tl.state is EmployeeState.OFF and clock_out.at == T + 9 * H


def test_amend_changes_kind():
  b = punch(BREAK_START, T + H)
  fix = PunchCorrection(
    id=uuid4(), punch_id=b.id, action=CorrectionAction.AMEND, new_kind=PunchKind.LUNCH_START, recorded_at=T + 2 * H
  )
  assert build_timeline([punch(CLOCK_IN, T), b], [fix]).state is EmployeeState.AT_LUNCH


def test_correction_for_unknown_punch_is_ignored():
  stray = PunchCorrection(id=uuid4(), punch_id=uuid4(), action=CorrectionAction.VOID, recorded_at=T)
  assert build_timeline([punch(CLOCK_IN, T)], [stray]).state is EmployeeState.WORKING


def test_duplicate_punch_ids_counted_once():
  p = punch(CLOCK_IN, T)
  assert build_timeline([p, p]).anomalies == ()


def test_same_instant_sorted_by_id():
  a, b = punch(CLOCK_IN, T, punch_id=UUID(int=1)), punch(CLOCK_OUT, T, punch_id=UUID(int=2))
  assert build_timeline([b, a]) == build_timeline([a, b]) and build_timeline([b, a]).state is EmployeeState.OFF


def test_mixed_employees_rejected():
  with pytest.raises(ValueError, match="employee"):
    build_timeline([punch(CLOCK_IN, T, employee_id=1), punch(CLOCK_OUT, T + H, employee_id=2)])


def hours(
  punches: list[Punch], *, now: datetime, settings: Settings = S, corrections: tuple[PunchCorrection, ...] = ()
) -> HoursTotals:
  """Totals over the week containing T."""
  start, end = week_bounds(T, NY)
  return total_hours(punches, corrections, settings=settings, tz=NY, start=start, end=end, now=now)


def test_hours_split_worked_paid_break_and_unpaid():
  day = [
    punch(CLOCK_IN, T),
    punch(BREAK_START, T + 2 * H),
    punch(BREAK_END, T + 2 * H + 15 * M),
    punch(LUNCH_START, T + 4 * H),
    punch(LUNCH_END, T + 4 * H + 30 * M),
    punch(CLOCK_OUT, T + 8 * H),
  ]
  assert hours(day, now=T + 9 * H) == HoursTotals(worked=7 * H + 15 * M, paid_break=15 * M, unpaid=30 * M)


def test_break_time_over_the_daily_allowance_is_unpaid():
  two_breaks = [
    punch(CLOCK_IN, T),
    punch(BREAK_START, T + H),
    punch(BREAK_END, T + H + 20 * M),
    punch(BREAK_START, T + 3 * H),
    punch(BREAK_END, T + 3 * H + 20 * M),
    punch(CLOCK_OUT, T + 8 * H),
  ]
  assert hours(two_breaks, now=T + 9 * H) == HoursTotals(worked=8 * H - 40 * M, paid_break=30 * M, unpaid=10 * M)
  one_long = [punch(CLOCK_IN, T), punch(BREAK_START, T + H), punch(BREAK_END, T + H + 45 * M), punch(CLOCK_OUT, T + 8 * H)]
  assert hours(one_long, now=T + 9 * H) == HoursTotals(worked=8 * H - 45 * M, paid_break=30 * M, unpaid=15 * M)
  no_allowance = Settings(paid_break_minutes=0)
  assert hours(one_long, now=T + 9 * H, settings=no_allowance) == HoursTotals(
    worked=8 * H - 45 * M, paid_break=timedelta(), unpaid=45 * M
  )


def test_a_break_counts_toward_the_day_it_started():
  # Tuesday 20:00 to Wednesday 02:00 local. The first break starts Tuesday and runs past midnight on Tuesday's allowance;
  # the second, after midnight, has Wednesday's. Were the first charged to Wednesday, the second would be unpaid.
  evening = datetime(2026, 10, 27, 20, 0, tzinfo=NY)
  night = [
    punch(CLOCK_IN, evening),
    punch(BREAK_START, evening + 3 * H + 50 * M),  # 23:50
    punch(BREAK_END, evening + 4 * H + 20 * M),  # 00:20
    punch(BREAK_START, evening + 5 * H),
    punch(BREAK_END, evening + 5 * H + 30 * M),
    punch(CLOCK_OUT, evening + 6 * H),
  ]
  assert hours(night, now=evening + 7 * H) == HoursTotals(worked=5 * H, paid_break=H, unpaid=timedelta())


def test_each_local_day_gets_its_own_allowance():
  def day(at: datetime) -> list[Punch]:
    return [punch(CLOCK_IN, at), punch(BREAK_START, at + H), punch(BREAK_END, at + H + 30 * M), punch(CLOCK_OUT, at + 4 * H)]

  assert hours(day(T) + day(T + 24 * H), now=T + 30 * H) == HoursTotals(worked=7 * H, paid_break=H, unpaid=timedelta())


def test_a_range_edge_clips_a_break_without_reallocating_it():
  # One hour of break from T: its first 30 minutes are paid and the rest unpaid, wherever the range starts.
  shift = [punch(CLOCK_IN, T - H), punch(BREAK_START, T), punch(BREAK_END, T + H), punch(CLOCK_OUT, T + 2 * H)]
  got = total_hours(shift, settings=S, tz=NY, start=T + 45 * M, end=T + 3 * H, now=T + 3 * H)
  assert got == HoursTotals(worked=H, paid_break=timedelta(), unpaid=15 * M)


def test_open_shift_and_open_break_run_until_now():
  assert hours([punch(CLOCK_IN, T)], now=T + 3 * H) == HoursTotals(worked=3 * H, paid_break=timedelta(), unpaid=timedelta())
  on_break = [punch(CLOCK_IN, T), punch(BREAK_START, T + H)]
  assert hours(on_break, now=T + 2 * H) == HoursTotals(worked=H, paid_break=30 * M, unpaid=30 * M)


def test_punches_stamped_after_now_dont_count_yet():
  # The automatic clock-out may be written before its stamp (§8.4); until then the shift is open and runs until now.
  with_future_out = [punch(CLOCK_IN, T), punch(CLOCK_OUT, T + 10 * H, automatic=True)]
  assert hours(with_future_out, now=T + 5 * H).worked == 5 * H
  assert hours(with_future_out, now=T + 12 * H).worked == 10 * H
  lunch = [punch(CLOCK_IN, T), punch(LUNCH_START, T + H), punch(LUNCH_END, T + 2 * H)]
  assert hours(lunch, now=T - H) == HoursTotals(worked=timedelta(), paid_break=timedelta(), unpaid=timedelta())
  assert hours(lunch, now=T + H + 30 * M) == HoursTotals(worked=H, paid_break=timedelta(), unpaid=30 * M)
  assert hours(lunch, now=T + 3 * H) == HoursTotals(worked=2 * H, paid_break=timedelta(), unpaid=H)


def test_corrections_apply_before_totalling():
  out = punch(CLOCK_OUT, T + 8 * H)
  amend = PunchCorrection(id=uuid4(), punch_id=out.id, action=CorrectionAction.AMEND, new_at=T + 6 * H, recorded_at=T + 9 * H)
  assert hours([punch(CLOCK_IN, T), out], now=T + 9 * H, corrections=(amend,)).worked == 6 * H


def test_build_timeline_as_of_drops_later_punches():
  later = punch(CLOCK_OUT, T + 10 * H, automatic=True)
  assert build_timeline([punch(CLOCK_IN, T), later], as_of=T + 5 * H).state is EmployeeState.WORKING
  assert build_timeline([punch(CLOCK_IN, T), later], as_of=T + 10 * H).state is EmployeeState.OFF
  # The effective time counts: a correction moving a punch past `as_of` drops it too.
  moved = PunchCorrection(id=uuid4(), punch_id=later.id, action=CorrectionAction.AMEND, new_at=T + 11 * H, recorded_at=T)
  assert build_timeline([punch(CLOCK_IN, T), later], [moved], as_of=T + 10 * H).state is EmployeeState.WORKING


def test_hours_split_at_the_week_boundary():
  sunday_night = datetime(2026, 11, 8, 22, 0, tzinfo=NY)  # 2026-11-09 03:00 UTC
  shift = [
    punch(CLOCK_IN, sunday_night),
    punch(LUNCH_START, sunday_night + 90 * M),  # 23:30, half an hour before the boundary
    punch(LUNCH_END, sunday_night + 150 * M),  # 00:30
    punch(CLOCK_OUT, sunday_night + 4 * H),
  ]
  this_week, next_week = week_bounds(sunday_night, NY), week_bounds(sunday_night + 4 * H, NY)
  assert this_week[1] == datetime(2026, 11, 9, 5, 0, tzinfo=UTC) == next_week[0]
  for bounds in (this_week, next_week):
    got = total_hours(shift, settings=S, tz=NY, start=bounds[0], end=bounds[1], now=sunday_night + 5 * H)
    assert got == HoursTotals(worked=90 * M, paid_break=timedelta(), unpaid=30 * M)


@pytest.mark.parametrize("field", ["start", "end", "now"])
def test_hours_reject_naive_datetimes(field: str):
  start, end = week_bounds(T, NY)
  args = {"start": start, "end": end, "now": T} | {field: datetime(2026, 10, 27, 15)}  # noqa: DTZ001
  with pytest.raises(ValueError, match="naive"):
    total_hours([punch(CLOCK_IN, T)], settings=S, tz=NY, start=args["start"], end=args["end"], now=args["now"])


def test_week_bounds_across_dst():
  assert week_bounds(datetime(2026, 10, 28, 12, 0, tzinfo=UTC), NY) == (
    datetime(2026, 10, 26, 4, 0, tzinfo=UTC),
    datetime(2026, 11, 2, 5, 0, tzinfo=UTC),
  )


def test_week_bounds_at_monday_midnight():
  monday = datetime(2026, 11, 9, 0, 0, tzinfo=NY)
  assert week_bounds(monday, NY)[0] == monday.astimezone(UTC)


def test_auto_clock_out_due():
  stamp = T + 10 * H
  assert auto_clock_out_due(build_timeline([punch(CLOCK_IN, T)]), stamp)
  assert auto_clock_out_due(build_timeline([punch(CLOCK_IN, T), punch(BREAK_START, T + H)]), stamp)
  assert not auto_clock_out_due(build_timeline([punch(CLOCK_IN, T + 11 * H)]), stamp)
  assert not auto_clock_out_due(build_timeline([punch(CLOCK_IN, T), punch(CLOCK_OUT, T + H)]), stamp)
  assert not auto_clock_out_due(build_timeline([]), stamp)
