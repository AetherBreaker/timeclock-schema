# Standard library imports
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

# Third party imports
import pytest
from pydantic import BaseModel

# First party imports
from timeclock_schema.enums import PunchKind, RuleAction
from timeclock_schema.models import Punch, RuleRow
from timeclock_schema.rules import (
  RuleContext,
  RuleValidationError,
  Violation,
  applicable_rules,
  evaluate,
  register_kind,
  registered_kinds,
  unregister_kind,
  validate_rule,
)
from timeclock_schema.shifts import build_timeline

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Iterator

CLOCK_IN, CLOCK_OUT = PunchKind.CLOCK_IN, PunchKind.CLOCK_OUT
BREAK_START, BREAK_END = PunchKind.BREAK_START, PunchKind.BREAK_END

T = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)  # Tuesday 08:00 EDT
H = timedelta(hours=1)


def punch(kind: PunchKind, at: datetime, *, automatic: bool = False, punch_id: UUID | None = None, employee_id: int = 1) -> Punch:
  return Punch(id=punch_id or uuid4(), employee_id=employee_id, kind=kind, at=at, instance_id="1", automatic=automatic)


def row(**kw: Any) -> RuleRow:
  fields: dict[str, Any] = {
    "id": 1,
    "kind": "test_max_breaks",
    "params": {"limit": 1},
    "group_id": 1,
    "on_violation": RuleAction.REFUSE,
    "updated_at": T,
  }
  return RuleRow.model_validate(fields | kw)


class MaxBreaks(BaseModel):
  limit: int


def _too_many_breaks(params: MaxBreaks, ctx: RuleContext) -> str | None:
  taken = sum(len(s.breaks) for s in ctx.timeline.shifts)
  return f"only {params.limit} breaks allowed" if ctx.proposed is PunchKind.BREAK_START and taken >= params.limit else None


@pytest.fixture
def max_breaks() -> Iterator[None]:
  register_kind("test_max_breaks", MaxBreaks, _too_many_breaks)
  yield
  unregister_kind("test_max_breaks")


def test_no_kinds_registered_in_version_1():
  assert registered_kinds() == ()


@pytest.mark.usefixtures("max_breaks")
def test_validate_known_kind():
  assert validate_rule(row()).params == MaxBreaks(limit=1)


def test_unknown_kind_fails_validation():
  with pytest.raises(RuleValidationError) as e:
    validate_rule(row(kind="nope"))
  assert e.value.rule_id == 1 and "unknown kind 'nope'" in str(e.value)


@pytest.mark.usefixtures("max_breaks")
def test_bad_params_fail_validation():
  with pytest.raises(RuleValidationError):
    validate_rule(row(params={"limit": "many"}))


@pytest.mark.usefixtures("max_breaks")
def test_registering_twice_raises():
  with pytest.raises(ValueError, match="test_max_breaks"):
    register_kind("test_max_breaks", MaxBreaks, _too_many_breaks)


@pytest.mark.usefixtures("max_breaks")
def test_employee_rule_replaces_group_rule_of_same_kind():
  group_rule = validate_rule(row(id=1, group_id=1))
  own_rule = validate_rule(row(id=2, group_id=None, employee_id=5, params={"limit": 3}))
  assert applicable_rules([group_rule, own_rule], employee_id=5, group_id=1) == (own_rule,)
  assert applicable_rules([group_rule, own_rule], employee_id=6, group_id=1) == (group_rule,)
  assert applicable_rules([group_rule], employee_id=6, group_id=2) == ()


@pytest.mark.usefixtures("max_breaks")
def test_evaluate_reports_violation_with_the_rows_action():
  tl = build_timeline([punch(CLOCK_IN, T), punch(BREAK_START, T + H), punch(BREAK_END, T + 2 * H)])
  ctx = RuleContext(employee_id=1, group_id=1, timeline=tl, proposed=PunchKind.BREAK_START, at=T + 3 * H)
  assert evaluate([validate_rule(row(on_violation=RuleAction.WARN))], ctx) == (
    Violation(rule_id=1, kind="test_max_breaks", action=RuleAction.WARN, message="only 1 breaks allowed"),
  )
  assert evaluate([validate_rule(row())], ctx.model_copy(update={"proposed": PunchKind.CLOCK_OUT})) == ()
