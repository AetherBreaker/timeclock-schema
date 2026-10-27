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


class OverrideReason(StrEnum):
  """`override_reason`."""

  NO_FINGERPRINT = "no_fingerprint"
  FINGERPRINT_SKIPPED = "fingerprint_skipped"


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
