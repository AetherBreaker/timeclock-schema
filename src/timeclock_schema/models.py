"""One model per table row (spec §4.2-4.3)."""

# Standard library imports
import base64
from datetime import time
from decimal import Decimal
from typing import Annotated, Any, Final, Self
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
from timeclock_schema.enums import (
  AutoClockOutFrequency,
  BypassReason,
  CorrectionAction,
  EmployeeRole,
  Finger,
  FingerprintChangeAction,
  PunchKind,
  RuleAction,
  ScanOutcome,
  ScanResult,
)

_MASK = "**********"

PHONE_PATTERN: Final = r"^[+]1[0-9]{10}$"
"""A stored phone number: a US number as `+1` and ten digits (`employees.phone`'s check)."""
EMAIL_PATTERN: Final = r"^[^@\s]+@[^@\s]+[.][^@\s]+$"
"""A stored email: something, `@`, something, a dot, something (`employees.email`'s check)."""


class Group(TimeclockModel):
  """`groups` row."""

  id: int
  name: str


class Employee(TimeclockModel):
  """`employees` row."""

  id: int
  first_name: str
  middle_name: str | None = None
  last_name: str
  pin_username: Annotated[str, StringConstraints(pattern=r"^[0-9]+$")]
  pin_hash: SecretStr
  role: EmployeeRole = EmployeeRole.EMPLOYEE
  active: bool = True
  group_id: int = 1
  phone: Annotated[str, StringConstraints(pattern=PHONE_PATTERN)] | None = None
  email: Annotated[str, StringConstraints(pattern=EMAIL_PATTERN)] | None = None
  fingerprint_exempt: bool = False
  """Logs in by PIN alone: no fingerprint step and no override (§6.4)."""
  updated_at: UtcDatetime

  @property
  def display_name(self) -> str:
    """First and last name, as the clock shows it; the middle name stays in the database."""
    return f"{self.first_name} {self.last_name}"

  # JSON input needs no validator: SecretStr accepts the revealed string as is.
  @field_serializer("pin_hash", when_used="json")
  def _serialize_pin_hash(self, value: SecretStr, info: SerializationInfo) -> str:
    return value.get_secret_value() if info.context and info.context.get("reveal_secrets") else _MASK


class Fingerprint(TimeclockModel):
  """`fingerprints` row; `template` is libfprint's serialized `FpPrint` (R15)."""

  id: UUID
  employee_id: int
  finger: Finger
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


class FingerprintScan(TimeclockModel):
  """`fingerprint_scans` row: one scan, with its best and next-best prints; `probe` and `image` are biometric."""

  id: UUID
  at: UtcDatetime
  instance_id: str
  result: ScanResult
  best_fingerprint_id: UUID | None = None
  best_employee_id: int | None = None
  best_finger: Finger | None = None
  best_score: int | None = None
  other_fingerprint_id: UUID | None = None
  other_employee_id: int | None = None
  other_finger: Finger | None = None
  other_score: int | None = None
  scores: dict[UUID, int] = Field(default_factory=dict)
  """Every print's score, by fingerprint id."""
  margin: int
  outcome: ScanOutcome | None = None
  punch_id: UUID | None = None
  retry_message: str | None = None
  probe: SecretBytes | None = None
  image: SecretBytes | None = None
  image_width: int | None = None
  image_height: int | None = None
  received_at: UtcDatetime | None = None
  db_login: str | None = None


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


class FingerprintChange(TimeclockModel):
  """`fingerprint_changes` row: a print added, replaced or removed, with the approving admin (none for a first add)."""

  id: UUID
  employee_id: int
  finger: Finger
  action: FingerprintChangeAction
  approved_by: int | None = None
  pin_only: bool | None = None
  at: UtcDatetime
  instance_id: str
  received_at: UtcDatetime | None = None
  db_login: str | None = None


class FingerprintBypass(TimeclockModel):
  """`fingerprint_bypasses` row: a PIN login without a fingerprint (R23.5).

  An override names its punch, its manager and whether the manager used a PIN alone; an admin's or exempt employee's
  login names neither of the last two, and a punch only when its PIN authorized one (the table's check).
  """

  id: UUID
  punch_id: UUID | None = None
  employee_id: int
  manager_id: int | None = None
  reason: BypassReason
  pin_only: bool | None = None
  at: UtcDatetime
  instance_id: str
  received_at: UtcDatetime | None = None
  db_login: str | None = None

  @model_validator(mode="after")
  def _check_reason(self) -> Self:
    if self.reason.is_override and None in {self.punch_id, self.manager_id, self.pin_only}:
      raise ValueError("an override names its punch, manager and pin_only")
    if not self.reason.is_override and (self.manager_id is not None or self.pin_only is not None):
      raise ValueError("a PIN login names no manager and no pin_only")
    return self


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


class Settings(TimeclockModel):
  """The one `config.settings` row; its `id` column is ignored."""

  auto_clock_out_enabled: bool = True
  auto_clock_out_run_time: LocalTime = time(23, 0)
  auto_clock_out_stamp_time: LocalTime = time(23, 0)
  auto_clock_out_frequency: AutoClockOutFrequency = AutoClockOutFrequency.DAILY
  lunches_enabled: bool = True
  breaks_enabled: bool = True
  """Business-wide switches for starting a lunch or a break (ending one is always offered); a per-group override may come later."""
  paid_break_minutes: Annotated[int, Field(ge=0)] = 30
  """Paid break time allowed per local day; break time beyond it is unpaid (§4.5)."""
  weekly_hours_alert: Annotated[Decimal, Field(gt=0, max_digits=6, decimal_places=2)] = Decimal(40)
  """The weekly hours alert's limit in hours, worked plus paid breaks (§6.11); it never restricts anyone."""
  save_scan_images: bool = True
  """Whether the clocks store each scan's image in `fingerprint_scans`."""
  fingerprint_match_margin: Annotated[int, Field(ge=0)] = 15
  """How far the best score must beat the best of a different employee's to count as a match."""
  registration_min_detail_points: Annotated[int, Field(ge=0)] = 20
  """Registration: the fewest detail points a press may have (a light or partial press has fewer)."""
  registration_max_detail_points: Annotated[int, Field(ge=0)] = 120
  """Registration: the most detail points a press may have (a smudged or noisy press has more); at least the fewest."""
  registration_centre_tolerance: Annotated[int, Field(ge=0)] = 100
  """Registration: how far in pixels a press's detail points' centroid may sit from the reader window's centre."""
  registration_agreement_bar: Annotated[int, Field(ge=0)] = 41
  """Registration: the score against another press of the same registration at which a press agrees with it."""
  registration_agreement_needed: Annotated[int, Field(ge=0, le=4)] = 1
  """Registration: how many of the other 4 kept presses each press must agree with."""
  registration_resemblance_bar: Annotated[int, Field(ge=0)] = 40
  """Registration: the score against another person's press at which a new press resembles it."""
  registration_extra_presses: Annotated[int, Field(ge=0)] = 5
  """Registration: how many failing presses may be replaced before it gives up."""

  @model_validator(mode="after")
  def _check_detail_points(self) -> Self:
    if self.registration_max_detail_points < self.registration_min_detail_points:
      raise ValueError("registration_max_detail_points must be at least registration_min_detail_points")
    return self


class AutoClockOutRun(TimeclockModel):
  """`backend.auto_clock_out_runs` row: one scheduled automatic clock-out; `ran_at` and `closed` stay None until it runs."""

  id: int
  scheduled_for: UtcDatetime
  scheduled_tz: str
  stamp: UtcDatetime
  ran_at: UtcDatetime | None = None
  closed: int | None = None
