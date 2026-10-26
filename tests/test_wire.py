# Standard library imports
from datetime import UTC, datetime
from uuid import uuid4

# Third party imports
import pytest
from pydantic import SecretBytes, ValidationError

# First party imports
from timeclock_schema.enums import PunchKind
from timeclock_schema.models import Fingerprint, Punch
from timeclock_schema.wire import LanFingerprint, LanPunch, Notification, dump_lan_record, load_lan_record

T = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)


def test_notification_parses_trigger_payloads():
  n = Notification.model_validate_json('{"table": "config.settings", "op": "UPDATE", "id": "true"}')
  assert (n.table, n.op, n.id) == ("config.settings", "UPDATE", "true")
  assert Notification.model_validate_json('{"table": "employees", "op": "DELETE", "id": "7"}').id == "7"
  with pytest.raises(ValidationError):
    Notification.model_validate_json('{"table": "overrides", "op": "INSERT", "id": "x"}')


def test_lan_punch_round_trip():
  p = Punch(id=uuid4(), employee_id=3, kind=PunchKind.CLOCK_IN, at=T, instance_id="1")
  assert load_lan_record(dump_lan_record(LanPunch(sender="1", punch=p))) == LanPunch(sender="1", punch=p)


def test_lan_fingerprint_carries_the_template():
  f = Fingerprint(id=uuid4(), employee_id=3, template=SecretBytes(b"\x01\x02tmpl"), registered_at=T, instance_id="2")
  back = load_lan_record(dump_lan_record(LanFingerprint(sender="2", fingerprint=f)))
  assert isinstance(back, LanFingerprint)
  assert back.fingerprint.template.get_secret_value() == b"\x01\x02tmpl"


def test_lan_record_rejects_unknown_type():
  with pytest.raises(ValidationError):
    load_lan_record(b'{"record_type": "override", "sender": "1"}')
