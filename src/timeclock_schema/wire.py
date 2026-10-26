"""Formats that cross a process boundary: the NOTIFY payload (spec §4.4) and the clock-to-clock LAN record (§6.7)."""

# Standard library imports
from typing import Annotated, Final, Literal

# Third party imports
from pydantic import Field, TypeAdapter

# First party imports
from timeclock_schema.base import REVEAL, TimeclockModel
from timeclock_schema.models import Fingerprint, Punch

NOTIFY_CHANNEL: Final = "timeclock"

NotifyTable = Literal[
  "employees", "groups", "fingerprints", "punches", "punch_corrections", "rules", "config.business_hours", "config.settings"
]
"""The tables 0001 notifies on, which a clock mirrors. `Notification.table` accepts any name (see there)."""


class Notification(TimeclockModel):
  """A trigger's `NOTIFY timeclock` payload. It carries only the row ID so a template never nears the 8,000-byte limit."""

  table: str
  """Usually a `NotifyTable`. Plain `str` for the reason behind `extra="ignore"`: a newer migration's notifying table must
  not make an older clock fail on every notification; receivers ignore tables they don't know."""
  op: Literal["INSERT", "UPDATE", "DELETE"]
  id: str
  """Always text (an integer, a UUID, or `"true"` for `config.settings`); the receiver converts per table."""


class LanPunch(TimeclockModel):
  """A punch sent to the other clocks."""

  record_type: Literal["punch"] = "punch"
  sender: str
  """The sender's `INSTANCE_ID`."""
  punch: Punch


class LanFingerprint(TimeclockModel):
  """A fingerprint registration sent to the other clocks."""

  record_type: Literal["fingerprint"] = "fingerprint"
  sender: str
  """The sender's `INSTANCE_ID`."""
  fingerprint: Fingerprint


LanRecord = Annotated[LanPunch | LanFingerprint, Field(discriminator="record_type")]
"""One LAN message: one JSON record per connection."""

_LAN_RECORD: Final = TypeAdapter[LanPunch | LanFingerprint](LanRecord)


def dump_lan_record(record: LanPunch | LanFingerprint) -> bytes:
  """Serializes `record` for the wire; secrets are revealed because the receiving clock needs the template."""
  return record.model_dump_json(context=REVEAL).encode()


def load_lan_record(data: bytes) -> LanPunch | LanFingerprint:
  """Parses one LAN message.

  Raises:
    ValidationError: `data` is not a valid punch or fingerprint record.
  """
  return _LAN_RECORD.validate_json(data)
