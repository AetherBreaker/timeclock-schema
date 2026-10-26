"""Punch-time rules (spec §4.6): a registry of rule kinds, row validation, precedence and evaluation.

Version 1 registers no kinds (R34), so every stored row fails validation until a kind ships.
"""

# Standard library imports
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

# Third party imports
from pydantic import BaseModel, ConfigDict, ValidationError

# First party imports
from timeclock_schema.base import TimeclockModel, UtcDatetime
from timeclock_schema.enums import PunchKind, RuleAction
from timeclock_schema.models import RuleRow
from timeclock_schema.shifts import Timeline

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Iterable


class RuleContext(TimeclockModel):
  """What a check sees: the employee, their timeline so far, and the punch they are attempting."""

  employee_id: int
  group_id: int
  timeline: Timeline
  proposed: PunchKind
  at: UtcDatetime


type RuleCheck[P: BaseModel] = Callable[[P, RuleContext], str | None]
"""Returns the violation message, or None when the proposed punch passes."""


@dataclass(frozen=True, slots=True)
class _Kind:
  params_model: type[BaseModel]
  check: RuleCheck[Any]


_KINDS: Final[dict[str, _Kind]] = {}


def register_kind[P: BaseModel](name: str, params_model: type[P], check: RuleCheck[P]) -> None:
  """Register a rule kind.

  Raises:
    ValueError: `name` is already registered.
  """
  if name in _KINDS:
    raise ValueError(f"rule kind {name!r} is already registered")
  _KINDS[name] = _Kind(params_model, check)


def unregister_kind(name: str) -> None:
  """Remove a rule kind.

  Raises:
    KeyError: `name` is not registered.
  """
  del _KINDS[name]


def registered_kinds() -> tuple[str, ...]:
  """The registered kind names, sorted."""
  return tuple(sorted(_KINDS))


class RuleValidationError(ValueError):
  """A rule row the registry can't use; the clock skips it and alerts (§4.6)."""

  def __init__(self, rule_id: int, reason: str) -> None:
    """Record the failing row's id and why it failed."""
    super().__init__(f"rule {rule_id}: {reason}")
    self.rule_id = rule_id
    self.reason = reason


class ValidatedRule(TimeclockModel):
  """A rule row with `params` parsed by its kind's model."""

  model_config = ConfigDict(arbitrary_types_allowed=True)

  row: RuleRow
  params: BaseModel


def validate_rule(row: RuleRow) -> ValidatedRule:
  """Parse `row.params` with its kind's model.

  This registry dispatch is the discriminated union on `kind` (R34.1): a static union can't be built from a registry
  that is empty in version 1.

  Raises:
    RuleValidationError: the kind is unknown or the params fail its model.
  """
  kind = _KINDS.get(row.kind)
  if kind is None:
    raise RuleValidationError(row.id, f"unknown kind {row.kind!r}")
  try:
    params = kind.params_model.model_validate(row.params)
  except ValidationError as e:
    raise RuleValidationError(row.id, str(e)) from e
  return ValidatedRule(row=row, params=params)


def applicable_rules(rules: Iterable[ValidatedRule], *, employee_id: int, group_id: int) -> tuple[ValidatedRule, ...]:
  """The employee's own rules plus their group's rules of every kind they have no own rule for, ordered by row id."""
  rules = tuple(rules)
  own = [r for r in rules if r.row.employee_id == employee_id]
  own_kinds = {r.row.kind for r in own}
  group = [r for r in rules if r.row.group_id == group_id and r.row.kind not in own_kinds]
  return tuple(sorted(own + group, key=lambda r: r.row.id))


class Violation(TimeclockModel):
  """A rule the proposed punch breaks; `action` says whether the clock refuses, warns or accepts (R34.2)."""

  rule_id: int
  kind: str
  action: RuleAction
  message: str


def evaluate(rules: Iterable[ValidatedRule], ctx: RuleContext) -> tuple[Violation, ...]:
  """Run each rule's check against `ctx`, in the given order."""
  violations: list[Violation] = []
  for rule in rules:
    message = _KINDS[rule.row.kind].check(rule.params, ctx)
    if message is not None:
      violations.append(Violation(rule_id=rule.row.id, kind=rule.row.kind, action=rule.row.on_violation, message=message))
  return tuple(violations)
