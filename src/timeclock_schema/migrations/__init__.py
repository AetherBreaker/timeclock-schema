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
    ValueError: The versions aren't exactly 1..n (a version is missing or repeated).
  """
  found = [
    Migration(version=int(match[1]), name=match[2], sql=entry.read_text(encoding="utf-8"))
    for entry in files(__package__).iterdir()
    if (match := _FILE_NAME.match(entry.name))
  ]
  found.sort(key=lambda m: m.version)
  if duplicates := sorted(v for v, count in Counter(m.version for m in found).items() if count > 1):
    raise ValueError(f"duplicate migration version {duplicates[0]}")
  for expected, m in enumerate(found, start=1):
    if m.version != expected:
      raise ValueError(f"missing migration version {expected}")
  return tuple(found)
