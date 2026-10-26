# Standard library imports
from typing import TYPE_CHECKING

# Third party imports
import pytest

# First party imports
import timeclock_schema.migrations
from timeclock_schema.migrations import load_migrations

if TYPE_CHECKING:
  # Standard library imports
  from pathlib import Path


def test_initial_migration_is_first():
  first = load_migrations()[0]
  assert (first.version, first.name) == (1, "initial")
  assert "CREATE TABLE punches" in first.sql


def test_versions_are_contiguous():
  assert [m.version for m in load_migrations()] == list(range(1, len(load_migrations()) + 1))


@pytest.mark.parametrize(
  ("names", "message"),
  [
    (["0001_initial.sql", "0003_later.sql"], "missing migration version 2"),
    (["0002_second.sql"], "missing migration version 1"),
    (["0001_initial.sql", "0001_again.sql"], "duplicate migration version 1"),
    (["0001_initial.sql", "0002_add-index.sql"], "0002_add-index.sql"),
    (["0001_initial.sql", "2_short.sql"], "2_short.sql"),
    (["0000_x.sql", "0001_initial.sql"], "0000_x.sql"),
  ],
)
def test_bad_migration_sets_are_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, names: list[str], message: str):
  for name in [*names, "README.txt", "__init__.py"]:
    (tmp_path / name).write_text("SELECT 1;")
  monkeypatch.setattr(timeclock_schema.migrations, "files", lambda _package: tmp_path)
  with pytest.raises(ValueError, match=message):
    load_migrations()
