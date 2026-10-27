"""One model per table row (spec §4.2-4.3)."""

# Standard library imports
import base64
from datetime import time
from typing import Annotated, Any, Self
from uuid import UUID

# Third party imports
from pydantic import (
  Field,
  SecretBytes,
  SecretStr,
  SerializationInfo,
  StringConstraints,
  field_serializer,
  field_validator,
  model_validator,
)

# First party imports
from timeclock_schema.base import LocalTime, TimeclockModel, UtcDatetime
from timeclock_schema.enums import AutoClockOutFrequency, CorrectionAction, EmployeeRole, OverrideReason, PunchKind, RuleAction

_MASK = "**********"


class Group(TimeclockModel):
  """`groups` row."""

  id: int
  name: str


class Employee(TimeclockModel):
  """`employees` row."""

  id: int
  name: str
  pin_username: Annotated[str, StringConstraints(pattern=r"^[0-9]+$")]
  pin_hash: SecretStr
  role: EmployeeRole = EmployeeRole.EMPLOYEE
  active: bool = True
  group_id: int = 1
  updated_at: UtcDatetime

  # JSON input needs no validator: SecretStr accepts the revealed string as is.
  @field_serializer("pin_hash", when_used="json")
  def _serialize_pin_hash(self, value: SecretStr, info: SerializationInfo) -> str:
    return value.get_secret_value() if info.context and info.context.get("reveal_secrets") else _MASK


class Fingerprint(TimeclockModel):
  """`fingerprints` row; `template` is libfprint's serialized `FpPrint` (R15)."""

  id: UUID
  employee_id: int
  template: SecretBytes
  registered_at: UtcDatetime
  instance_id: str
  db_login: str | None = None

  # Revealed JSON carries the template as base64, since raw bytes aren't valid JSON text; the validator reverses it.
  @field_serializer("template", when_used="json")
  def _serialize_template(self, value: SecretBytes, info: SerializationInfo) -> str:
    return base64.b64encode(value.get_secret_value()).decode() if info.context and info.context.get("reveal_secrets") else _MASK

  @field_validator("template", mode="before")
  @classmethod
  def _decode_template(cls, value: object) -> object:
    return base64.b64decode(value, validate=True) if isinstance(value, str) else value


class Punch(TimeclockModel):
  """`punches` row."""

  id: UUID
  employee_id: int
  kind: PunchKind
  at: UtcDatetime
  instance_id: str
  automatic: bool = False
  received_at: UtcDatetime | None = None
  db_login: str | None = None


class PunchCorrection(TimeclockModel):
  """`punch_corrections` row; for each punch the newest correction wins."""

  id: UUID
  punch_id: UUID
  action: CorrectionAction
  new_at: UtcDatetime | None = None
  new_kind: PunchKind | None = None
  note: str = ""
  recorded_at: UtcDatetime

  @model_validator(mode="after")
  def _check_new_values(self) -> Self:
    has_new_value = self.new_at is not None or self.new_kind is not None
    if self.action is CorrectionAction.AMEND and not has_new_value:
      raise ValueError("an amend needs new_at or new_kind")
    if self.action is CorrectionAction.VOID and has_new_value:
      raise ValueError("a void takes neither new_at nor new_kind")
    return self


class Override(TimeclockModel):
  """`overrides` row: a manager's authorization of one punch (R23.5)."""

  id: UUID
  punch_id: UUID
  employee_id: int
  manager_id: int
  reason: OverrideReason
  pin_only: bool
  at: UtcDatetime
  instance_id: str
  received_at: UtcDatetime | None = None
  db_login: str | None = None


class RuleRow(TimeclockModel):
  """`rules` row, targeting exactly one group or one employee (R34)."""

  id: int
  kind: str
  params: dict[str, Any] = Field(default_factory=dict)
  group_id: int | None = None
  employee_id: int | None = None
  on_violation: RuleAction
  updated_at: UtcDatetime

  @model_validator(mode="after")
  def _check_one_target(self) -> Self:
    if (self.group_id is None) == (self.employee_id is None):
      raise ValueError("a rule needs exactly one of group_id and employee_id")
    return self


class BusinessHours(TimeclockModel):
  """`config.business_hours` row (R40.3)."""

  id: int
  weekday: Annotated[int, Field(ge=1, le=7)]
  """ISO weekday, 1 = Monday."""
  opens: LocalTime
  closes: LocalTime

  @model_validator(mode="after")
  def _check_order(self) -> Self:
    if self.opens >= self.closes:
      raise ValueError("opens must be earlier than closes")
    return self


class AutoClockOut(TimeclockModel):
  """The automatic clock-out's run and stamp times, checked against each other (R41.3)."""

  run_time: LocalTime
  stamp_time: LocalTime

  @model_validator(mode="after")
  def _check_stamp_not_after_run(self) -> Self:
    if self.stamp_time > self.run_time:
      raise ValueError("auto_clock_out_stamp_time is later than auto_clock_out_run_time")
    return self


class Settings(TimeclockModel):
  """The one `config.settings` row; its `id` column is ignored.

  The two auto-clock-out times aren't checked here, so a clock can still read the subtraction switches from a badly edited
  row; the backend checks them through `auto_clock_out()` (R41.3).
  """

  auto_clock_out_enabled: bool = True
  auto_clock_out_run_time: LocalTime = time(23, 0)
  auto_clock_out_stamp_time: LocalTime = time(23, 0)
  auto_clock_out_frequency: AutoClockOutFrequency = AutoClockOutFrequency.DAILY
  subtract_lunch: bool = True
  subtract_breaks: bool = False
  lunches_enabled: bool = True
  breaks_enabled: bool = True
  """Business-wide switches for starting a lunch or a break (ending one is always offered); a per-group override may come later."""

  def auto_clock_out(self) -> AutoClockOut:
    """Return the checked auto-clock-out times.

    Raises:
      ValidationError: The stamp time is later than the run time.
    """
    return AutoClockOut(run_time=self.auto_clock_out_run_time, stamp_time=self.auto_clock_out_stamp_time)


class AutoClockOutRun(TimeclockModel):
  """`backend.auto_clock_out_runs` row: one scheduled automatic clock-out; `ran_at` and `closed` stay None until it runs."""

  id: int
  scheduled_for: UtcDatetime
  scheduled_tz: str
  stamp: UtcDatetime
  ran_at: UtcDatetime | None = None
  closed: int | None = None
