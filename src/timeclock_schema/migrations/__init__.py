"""The SQL migrations, shipped as package data, which the backend applies at startup (spec §8.3).

Each file holds no `BEGIN`/`COMMIT`: the backend's runner owns `schema_migrations` and runs each file in one transaction.
"""

# Standard library imports
import re
from collections import Counter
from importlib.resources import files
from typing import Final

# First party imports
from timeclock_schema.base import TimeclockModel

_FILE_NAME: Final = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")


class Migration(TimeclockModel):
  """One migration file: `0001_initial.sql` is `version=1, name="initial"`."""

  version: int
  name: str
  sql: str


def load_migrations() -> tuple[Migration, ...]:
  """Every migration in the package, by version.

  Raises:
    ValueError: A `.sql` file is misnamed or numbered 0000, or the versions aren't exactly 1..n (a version is missing or
      repeated). A misnamed file is an error, not skipped, so a migration can't silently go unapplied.
  """
  found: list[Migration] = []
  for entry in files(__package__).iterdir():
    if not entry.name.endswith(".sql"):
      continue
    if not (match := _FILE_NAME.match(entry.name)) or int(match[1]) < 1:
      raise ValueError(f"migration file {entry.name!r} doesn't match NNNN_name.sql (NNNN from 0001, name in [a-z0-9_])")
    found.append(Migration(version=int(match[1]), name=match[2], sql=entry.read_text(encoding="utf-8")))
  found.sort(key=lambda m: m.version)
  if duplicates := sorted(v for v, count in Counter(m.version for m in found).items() if count > 1):
    raise ValueError(f"duplicate migration version {duplicates[0]}")
  for expected, m in enumerate(found, start=1):
    if m.version != expected:
      raise ValueError(f"missing migration version {expected}")
  return tuple(found)
