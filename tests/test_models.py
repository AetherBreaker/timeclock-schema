# Standard library imports
import base64
from datetime import UTC, datetime, time, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

# Third party imports
import pytest
from pydantic import ValidationError

# First party imports
from timeclock_schema.base import REVEAL
from timeclock_schema.enums import CorrectionAction, PunchKind, RuleAction
from timeclock_schema.models import BusinessHours, Employee, Fingerprint, Group, Punch, PunchCorrection, RuleRow, Settings

T = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)


def test_punch_normalizes_to_utc():
  p = Punch(
    id=uuid4(),
    employee_id=1,
    kind=PunchKind.CLOCK_IN,
    at=datetime(2026, 10, 27, 8, 0, tzinfo=ZoneInfo("America/New_York")),
    instance_id="1",
  )
  assert p.at == T and p.at.utcoffset() == timedelta(0)


def test_naive_datetime_rejected():
  with pytest.raises(ValidationError):
    Punch(id=uuid4(), employee_id=1, kind=PunchKind.CLOCK_IN, at=datetime(2026, 10, 27, 12, 0), instance_id="1")  # noqa: DTZ001


def test_local_time_rejects_tzinfo():
  with pytest.raises(ValidationError):
    BusinessHours(id=1, weekday=1, opens=time(8, tzinfo=UTC), closes=time(17))


def test_business_hours_bounds():
  BusinessHours(id=1, weekday=7, opens=time(8), closes=time(17))
  for bad in ({"weekday": 0}, {"weekday": 8}, {"opens": time(17), "closes": time(8)}):
    with pytest.raises(ValidationError):
      BusinessHours.model_validate({"id": 1, "weekday": 1, "opens": time(8), "closes": time(17)} | bad)


def test_correction_amend_needs_a_new_value_and_void_takes_none():
  base = {"id": uuid4(), "punch_id": uuid4(), "recorded_at": T}
  PunchCorrection.model_validate(base | {"action": CorrectionAction.AMEND, "new_at": T})
  PunchCorrection.model_validate(base | {"action": CorrectionAction.VOID})
  with pytest.raises(ValidationError):
    PunchCorrection.model_validate(base | {"action": CorrectionAction.AMEND})
  with pytest.raises(ValidationError):
    PunchCorrection.model_validate(base | {"action": CorrectionAction.VOID, "new_kind": PunchKind.CLOCK_OUT})


def test_rule_row_needs_exactly_one_target():
  base = {"id": 1, "kind": "k", "on_violation": RuleAction.WARN, "updated_at": T}
  RuleRow.model_validate(base | {"group_id": 1})
  with pytest.raises(ValidationError):
    RuleRow.model_validate(base)
  with pytest.raises(ValidationError):
    RuleRow.model_validate(base | {"group_id": 1, "employee_id": 2})


def test_employee_pin_username_digits_only():
  with pytest.raises(ValidationError):
    Employee.model_validate({"id": 1, "name": "A", "pin_username": "12a", "pin_hash": "$2a$06$x", "updated_at": T})


def test_fingerprint_template_masked_unless_revealed():
  f = Fingerprint.model_validate(
    {"id": uuid4(), "employee_id": 1, "template": b"\x00secret-bytes", "registered_at": T, "instance_id": "1"}
  )
  encoded = base64.b64encode(b"\x00secret-bytes").decode()
  for text in (repr(f), str(f), f.model_dump_json()):
    assert "secret-bytes" not in text and encoded not in text
  assert encoded in f.model_dump_json(context=REVEAL)
  assert Fingerprint.model_validate_json(f.model_dump_json(context=REVEAL)).template.get_secret_value() == b"\x00secret-bytes"


def test_pin_hash_masked_unless_revealed():
  e = Employee.model_validate({"id": 1, "name": "A", "pin_username": "12", "pin_hash": "$2a$06$abcdef", "updated_at": T})
  assert "abcdef" not in repr(e) and "abcdef" not in e.model_dump_json()
  assert "abcdef" in e.model_dump_json(context=REVEAL)


def test_settings_defaults_match_the_seed_row():
  s = Settings()
  assert (s.auto_clock_out_enabled, s.auto_clock_out_run_time, s.auto_clock_out_stamp_time, s.subtract_lunch, s.subtract_breaks) == (
    True,
    time(23),
    time(23),
    True,
    False,
  )


def test_auto_clock_out_rejects_stamp_after_run():
  assert Settings(auto_clock_out_run_time=time(23), auto_clock_out_stamp_time=time(18)).auto_clock_out().stamp_time == time(18)
  assert Settings(auto_clock_out_run_time=time(23), auto_clock_out_stamp_time=time(23)).auto_clock_out().stamp_time == time(23)
  bad = Settings(auto_clock_out_run_time=time(2), auto_clock_out_stamp_time=time(23))  # the row itself still loads
  with pytest.raises(ValidationError):
    bad.auto_clock_out()


def test_unknown_columns_are_ignored():
  assert Group.model_validate({"id": 1, "name": "default", "added_later": 5}) == Group(id=1, name="default")
