# Standard library imports
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

# Third party imports
import pytest

# First party imports
from timeclock_schema.enums import CorrectionAction, PunchKind
from timeclock_schema.models import Punch, PunchCorrection
from timeclock_schema.shifts import (
  VALID_NEXT,
  EmployeeState,
  Interval,
  auto_clock_out_due,
  build_timeline,
  week_bounds,
  worked_time,
)

CLOCK_IN, CLOCK_OUT = PunchKind.CLOCK_IN, PunchKind.CLOCK_OUT
BREAK_START, BREAK_END = PunchKind.BREAK_START, PunchKind.BREAK_END
LUNCH_START, LUNCH_END = PunchKind.LUNCH_START, PunchKind.LUNCH_END

T = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)  # Tuesday 08:00 EDT
H = timedelta(hours=1)
M = timedelta(minutes=1)
NY = ZoneInfo("America/New_York")


def punch(kind: PunchKind, at: datetime, *, automatic: bool = False, punch_id: UUID | None = None, employee_id: int = 1) -> Punch:
  return Punch(id=punch_id or uuid4(), employee_id=employee_id, kind=kind, at=at, instance_id="1", automatic=automatic)


def test_valid_next_matches_spec_table():
  assert VALID_NEXT == {
    EmployeeState.OFF: (PunchKind.CLOCK_IN,),
    EmployeeState.WORKING: (PunchKind.CLOCK_OUT, PunchKind.BREAK_START, PunchKind.LUNCH_START),
    EmployeeState.ON_BREAK: (PunchKind.BREAK_END, PunchKind.CLOCK_OUT),
    EmployeeState.AT_LUNCH: (PunchKind.LUNCH_END, PunchKind.CLOCK_OUT),
  }


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


def test_real_clock_out_overrides_automatic_either_order():
  later_real = build_timeline([punch(CLOCK_IN, T), punch(CLOCK_OUT, T + 10 * H, automatic=True), punch(CLOCK_OUT, T + 11 * H)])
  later_out = later_real.shifts[0].clock_out
  assert later_out is not None
  assert later_out.at == T + 11 * H and not later_out.automatic and later_real.anomalies == ()
  earlier_real = build_timeline([punch(CLOCK_IN, T), punch(CLOCK_OUT, T + 8 * H), punch(CLOCK_OUT, T + 10 * H, automatic=True)])
  earlier_out = earlier_real.shifts[0].clock_out
  assert earlier_out is not None
  assert earlier_out.at == T + 8 * H and earlier_real.anomalies == ()


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


def test_worked_time_lunch_and_break_switches():
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
  week = week_bounds(T, NY)
  assert worked_time(tl, *week, now=T + 9 * H, subtract_lunch=True, subtract_breaks=False) == 7 * H + 30 * M
  assert worked_time(tl, *week, now=T + 9 * H, subtract_lunch=True, subtract_breaks=True) == 7 * H + 15 * M
  assert worked_time(tl, *week, now=T + 9 * H, subtract_lunch=False, subtract_breaks=False) == 8 * H


def test_worked_time_open_shift_counts_until_now():
  week = week_bounds(T, NY)
  assert worked_time(build_timeline([punch(CLOCK_IN, T)]), *week, now=T + 3 * H, subtract_lunch=True, subtract_breaks=False) == 3 * H
  on_break = build_timeline([punch(CLOCK_IN, T), punch(BREAK_START, T + H)])
  assert worked_time(on_break, *week, now=T + 2 * H, subtract_lunch=True, subtract_breaks=True) == H


def test_worked_time_splits_at_week_boundary():
  sunday_night = datetime(2026, 11, 8, 22, 0, tzinfo=NY)  # 2026-11-09 03:00 UTC
  tl = build_timeline([punch(CLOCK_IN, sunday_night), punch(CLOCK_OUT, sunday_night + 4 * H)])
  this_week, next_week = week_bounds(sunday_night, NY), week_bounds(sunday_night + 4 * H, NY)
  assert this_week[1] == datetime(2026, 11, 9, 5, 0, tzinfo=UTC) == next_week[0]
  for bounds in (this_week, next_week):
    assert worked_time(tl, *bounds, now=sunday_night + 5 * H, subtract_lunch=True, subtract_breaks=False) == 2 * H


def test_week_bounds_across_dst():
  assert week_bounds(datetime(2026, 10, 28, 12, 0, tzinfo=UTC), NY) == (
    datetime(2026, 10, 26, 4, 0, tzinfo=UTC),
    datetime(2026, 11, 2, 5, 0, tzinfo=UTC),
  )


def test_week_bounds_at_monday_midnight():
  monday = datetime(2026, 11, 9, 0, 0, tzinfo=NY)
  assert week_bounds(monday, NY)[0] == monday.astimezone(UTC)


def test_worked_time_rejects_naive_now():
  with pytest.raises(ValueError, match="naive"):
    worked_time(
      build_timeline([punch(CLOCK_IN, T)]),
      *week_bounds(T, NY),
      now=datetime(2026, 10, 27, 15),  # noqa: DTZ001
      subtract_lunch=True,
      subtract_breaks=False,
    )


def test_auto_clock_out_due():
  stamp = T + 10 * H
  assert auto_clock_out_due(build_timeline([punch(CLOCK_IN, T)]), stamp)
  assert auto_clock_out_due(build_timeline([punch(CLOCK_IN, T), punch(BREAK_START, T + H)]), stamp)
  assert not auto_clock_out_due(build_timeline([punch(CLOCK_IN, T + 11 * H)]), stamp)
  assert not auto_clock_out_due(build_timeline([punch(CLOCK_IN, T), punch(CLOCK_OUT, T + H)]), stamp)
  assert not auto_clock_out_due(build_timeline([]), stamp)
