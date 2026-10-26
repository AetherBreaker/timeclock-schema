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
