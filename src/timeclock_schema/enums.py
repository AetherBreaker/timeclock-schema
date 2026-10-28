"""Enums whose values are the labels of the matching Postgres enum types."""

# Standard library imports
from enum import StrEnum


class PunchKind(StrEnum):
  """`punch_kind`."""

  CLOCK_IN = "clock_in"
  CLOCK_OUT = "clock_out"
  BREAK_START = "break_start"
  BREAK_END = "break_end"
  LUNCH_START = "lunch_start"
  LUNCH_END = "lunch_end"


class CorrectionAction(StrEnum):
  """`correction_action`."""

  VOID = "void"
  AMEND = "amend"


class BypassReason(StrEnum):
  """`bypass_reason`: why a PIN login went without a fingerprint (spec §4.2)."""

  OVERRIDE_NO_FINGERPRINT = "override_no_fingerprint"
  """A manager override for an employee with no registered print."""
  OVERRIDE_FINGERPRINT_SKIPPED = "override_fingerprint_skipped"
  """A manager override for an employee who has a print but didn't scan it."""
  ADMIN_PIN_LOGIN = "admin_pin_login"
  EXEMPT_PIN_LOGIN = "exempt_pin_login"
  """A `fingerprint_exempt` employee's PIN login."""

  @property
  def is_override(self) -> bool:
    """Whether a manager approved it, so the row names the manager and the punch."""
    return self in {BypassReason.OVERRIDE_NO_FINGERPRINT, BypassReason.OVERRIDE_FINGERPRINT_SKIPPED}


class RuleAction(StrEnum):
  """`rule_action`: what a rule violation does to the punch (R34.2)."""

  REFUSE = "refuse"
  WARN = "warn"
  ACCEPT = "accept"


class EmployeeRole(StrEnum):
  """`employee_role`, lowest to highest: an admin may do everything a manager may (approve overrides, R23.6)."""

  EMPLOYEE = "employee"
  MANAGER = "manager"
  ADMIN = "admin"


class AutoClockOutFrequency(StrEnum):
  """`auto_clock_out_frequency`: how often the automatic clock-out runs; daily only for now."""

  DAILY = "daily"


class Finger(StrEnum):
  """`finger`: which finger a print is, one per finger per person."""

  LEFT_THUMB = "left_thumb"
  LEFT_INDEX = "left_index"
  LEFT_MIDDLE = "left_middle"
  LEFT_RING = "left_ring"
  LEFT_LITTLE = "left_little"
  RIGHT_THUMB = "right_thumb"
  RIGHT_INDEX = "right_index"
  RIGHT_MIDDLE = "right_middle"
  RIGHT_RING = "right_ring"
  RIGHT_LITTLE = "right_little"


class FingerprintChangeAction(StrEnum):
  """`fingerprint_change_action`."""

  ADD = "add"
  REPLACE = "replace"
  REMOVE = "remove"
