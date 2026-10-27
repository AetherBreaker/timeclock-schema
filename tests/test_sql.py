"""SQL tests for `0001_initial.sql`, against a real PostgreSQL 18.

The test database is a throwaway PostgreSQL 18 in Docker Desktop on the workstation (PowerShell):

    docker run --rm -d --name timeclock-schema-pg -e POSTGRES_PASSWORD=test -p 127.0.0.1:55432:5432 postgres:18
    $env:TIMECLOCK_SCHEMA_TEST_DSN = "postgresql://postgres:test@127.0.0.1:55432/postgres"

Stop it afterwards with `docker stop timeclock-schema-pg` (`--rm` deletes it). Without the env var every test here is
skipped. Each test gets its own freshly migrated database, dropped afterwards.
"""

# Standard library imports
import os
from contextlib import contextmanager
from datetime import UTC, datetime, time, timedelta
from typing import TYPE_CHECKING, LiteralString, cast, get_args
from uuid import uuid4

# Third party imports
import psycopg
import pytest
from psycopg import errors, sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row

# First party imports
from timeclock_schema.migrations import load_migrations
from timeclock_schema.models import (
  AutoClockOutRun,
  BusinessHours,
  Employee,
  Fingerprint,
  Group,
  Override,
  Punch,
  PunchCorrection,
  RuleRow,
  Settings,
)
from timeclock_schema.wire import NOTIFY_CHANNEL, Notification, NotifyTable

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Generator, Iterator

  # First party imports
  from timeclock_schema.base import TimeclockModel

pytestmark = pytest.mark.skipif(
  not os.environ.get("TIMECLOCK_SCHEMA_TEST_DSN"), reason="needs TIMECLOCK_SCHEMA_TEST_DSN (see module docstring)"
)

T = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)


@contextmanager
def migrated_database(admin_dsn: str) -> Generator[str]:
  """Creates a database, applies every migration (one transaction each), yields its DSN, and drops it."""
  name = f"tcs_{uuid4().hex}"
  with psycopg.connect(admin_dsn, autocommit=True) as admin:
    admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
  try:
    dsn = make_conninfo(admin_dsn, dbname=name)
    with psycopg.connect(dsn) as conn:
      for m in load_migrations():
        with conn.transaction():
          conn.execute(cast("LiteralString", m.sql))  # package data, not user input
    yield dsn
  finally:
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
      admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


@pytest.fixture
def admin_dsn() -> str:
  return os.environ["TIMECLOCK_SCHEMA_TEST_DSN"]


@pytest.fixture
def fresh_db(admin_dsn: str) -> Iterator[str]:
  with migrated_database(admin_dsn) as dsn:
    yield dsn


@pytest.fixture
def clock_login(admin_dsn: str, fresh_db: str) -> Iterator[str]:
  # Torn down before `fresh_db`; the test's connections as this role are closed by then, and it owns nothing.
  role = f"tcs_clock_{uuid4().hex}"
  with psycopg.connect(admin_dsn, autocommit=True) as admin:
    admin.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD 'pw' IN ROLE timeclock_clock_access").format(sql.Identifier(role)))
  try:
    yield make_conninfo(fresh_db, user=role, password="pw")
  finally:
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
      admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


def _add_employee(c: psycopg.Connection, name: str = "Ann", pin_username: str = "12") -> int:
  row = c.execute(
    "INSERT INTO employees (name, pin_username, pin_hash) VALUES (%s, %s, crypt('1234', gen_salt('bf'))) RETURNING id",
    (name, pin_username),
  ).fetchone()
  assert row is not None
  return row[0]


def _add_punch(c: psycopg.Connection, employee_id: int) -> str:
  punch_id = str(uuid4())
  c.execute(
    "INSERT INTO punches (id, employee_id, kind, at, instance_id) VALUES (%s, %s, 'clock_in', %s, '1')", (punch_id, employee_id, T)
  )
  return punch_id


def test_migration_applies_in_two_databases(fresh_db: str, admin_dsn: str):
  # A second database on the same server: the role already exists, and nothing else may collide.
  with migrated_database(admin_dsn) as dsn, psycopg.connect(dsn) as c:
    assert c.execute("SELECT count(*) FROM groups").fetchone() == (1,)


def test_seed_rows(fresh_db: str):
  with psycopg.connect(fresh_db) as c:
    assert c.execute("SELECT id, name FROM groups").fetchall() == [(1, "default")]
    assert c.execute(
      "SELECT auto_clock_out_enabled, auto_clock_out_run_time, auto_clock_out_stamp_time, subtract_lunch, subtract_breaks,"
      " auto_clock_out_frequency, lunches_enabled, breaks_enabled FROM config.settings"
    ).fetchall() == [(True, time(23), time(23), True, False, "daily", True, True)]
    assert c.execute("SELECT count(*) FROM config.business_hours").fetchone() == (0,)


def test_employee_defaults_and_updated_at(fresh_db: str):
  with psycopg.connect(fresh_db, autocommit=True) as c:
    with pytest.raises(errors.CheckViolation):
      c.execute("INSERT INTO employees (name, pin_username, pin_hash) VALUES ('Ann', '12a', crypt('1234', gen_salt('bf')))")
    row = c.execute(
      "INSERT INTO employees (name, pin_username, pin_hash) VALUES ('Ann', '12', crypt('1234', gen_salt('bf')))"
      " RETURNING id, group_id, role, active, updated_at"
    ).fetchone()
    assert row is not None
    employee_id, group_id, role, active, t0 = row
    assert (group_id, role, active) == (1, "employee", True)
    assert c.execute("SELECT crypt('1234', pin_hash) = pin_hash FROM employees WHERE id = %s", (employee_id,)).fetchone() == (True,)
    updated = c.execute("UPDATE employees SET name = 'Ann B' WHERE id = %s RETURNING updated_at", (employee_id,)).fetchone()
    assert updated is not None
    assert updated[0] > t0


def test_employee_role_values(fresh_db: str):
  with psycopg.connect(fresh_db, autocommit=True) as c:
    employee_id = _add_employee(c)
    for role in ("manager", "admin", "employee"):
      c.execute("UPDATE employees SET role = %s WHERE id = %s", (role, employee_id))
    with pytest.raises(errors.InvalidTextRepresentation):
      c.execute("UPDATE employees SET role = 'owner' WHERE id = %s", (employee_id,))
    with pytest.raises(errors.UndefinedColumn):
      c.execute("SELECT is_manager FROM employees")
    # Declared lowest to highest, so SQL can ask "manager or above" with `role >= 'manager'`.
    assert c.execute("SELECT enum_range(NULL::employee_role)::text").fetchone() == ("{employee,manager,admin}",)


def test_notify_payloads(fresh_db: str):
  with psycopg.connect(fresh_db, autocommit=True) as listener, psycopg.connect(fresh_db, autocommit=True) as c:
    listener.execute(f"LISTEN {NOTIFY_CHANNEL}")
    employee_id = str(_add_employee(c))
    c.execute("UPDATE employees SET name = 'Ann B' WHERE id = %s", (employee_id,))
    c.execute("DELETE FROM employees WHERE id = %s", (employee_id,))
    c.execute("UPDATE config.settings SET subtract_breaks = true")
    got = [
      (n.table, n.op, n.id)
      for n in (Notification.model_validate_json(raw.payload) for raw in listener.notifies(timeout=5, stop_after=4))
    ]
  assert got == [
    ("employees", "INSERT", employee_id),
    ("employees", "UPDATE", employee_id),
    ("employees", "DELETE", employee_id),
    ("config.settings", "UPDATE", "true"),
  ]


def test_notify_triggers_cover_notify_table(fresh_db: str):
  with psycopg.connect(fresh_db) as c:
    rows = c.execute(
      "SELECT DISTINCT CASE WHEN n.nspname = 'public' THEN r.relname ELSE n.nspname || '.' || r.relname END"
      " FROM pg_trigger t JOIN pg_class r ON r.oid = t.tgrelid JOIN pg_namespace n ON n.oid = r.relnamespace"
      " JOIN pg_proc p ON p.oid = t.tgfoid WHERE p.proname = 'notify_change' AND NOT t.tgisinternal"
    ).fetchall()
  assert {table for (table,) in rows} == set(get_args(NotifyTable))


def test_clock_login_can_insert_and_read_only(fresh_db: str, clock_login: str):
  with psycopg.connect(fresh_db, autocommit=True) as admin:
    employee_id = _add_employee(admin)
    manager_id = _add_employee(admin, "Bob", "34")
  role = conninfo_to_dict(clock_login)["user"]
  punch_id, override_id, fingerprint_id = uuid4(), uuid4(), uuid4()
  with psycopg.connect(clock_login, autocommit=True) as c:
    for _ in range(2):
      c.execute(
        "INSERT INTO punches (id, employee_id, kind, at, instance_id) VALUES (%s, %s, 'clock_in', %s, '1') ON CONFLICT (id) DO NOTHING",
        (punch_id, employee_id, T),
      )
      c.execute(
        "INSERT INTO overrides (id, punch_id, employee_id, manager_id, reason, pin_only, at, instance_id)"
        " VALUES (%s, %s, %s, %s, 'no_fingerprint', false, %s, '1') ON CONFLICT (id) DO NOTHING",
        (override_id, punch_id, employee_id, manager_id, T),
      )
      c.execute(
        "INSERT INTO fingerprints (id, employee_id, template, registered_at, instance_id)"
        " VALUES (%s, %s, %s, %s, '1') ON CONFLICT (id) DO NOTHING",
        (fingerprint_id, employee_id, b"tmpl", T),
      )
    assert c.execute("SELECT count(*), min(db_login) FROM punches").fetchone() == (1, role)
    assert c.execute("SELECT count(*), min(db_login) FROM fingerprints").fetchone() == (1, role)

    forbidden = [
      ("INSERT INTO punches (id, employee_id, kind, at, instance_id, db_login) VALUES (%s, %s, 'clock_in', %s, '1', 'x')", True),
      ("INSERT INTO punches (id, employee_id, kind, at, instance_id, automatic) VALUES (%s, %s, 'clock_in', %s, '1', true)", True),
      ("UPDATE punches SET at = now()", False),
      ("DELETE FROM punches", False),
      ("SELECT * FROM overrides", False),
      ("INSERT INTO employees (name, pin_username, pin_hash) VALUES ('Eve', '99', crypt('1234', gen_salt('bf')))", False),
      ("UPDATE config.settings SET subtract_breaks = true", False),
    ]
    for sql, takes_punch in forbidden:
      with pytest.raises(errors.InsufficientPrivilege):
        c.execute(sql, (uuid4(), employee_id, T) if takes_punch else None)  # pyright: ignore[reportArgumentType]

  with psycopg.connect(fresh_db) as admin:
    assert admin.execute("SELECT count(*), min(db_login) FROM overrides").fetchone() == (1, role)


def test_owner_can_update_and_delete_punches(fresh_db: str):
  with psycopg.connect(fresh_db, autocommit=True) as c:
    punch_id = _add_punch(c, _add_employee(c))
    assert c.execute("UPDATE punches SET at = at + interval '1 minute' WHERE id = %s", (punch_id,)).rowcount == 1
    assert c.execute("DELETE FROM punches WHERE id = %s", (punch_id,)).rowcount == 1


def test_correction_check_constraint(fresh_db: str):
  with psycopg.connect(fresh_db, autocommit=True) as c:
    punch_id = _add_punch(c, _add_employee(c))
    with pytest.raises(errors.CheckViolation):
      c.execute("INSERT INTO punch_corrections (punch_id, action, new_at) VALUES (%s, 'void', %s)", (punch_id, T))
    with pytest.raises(errors.CheckViolation):
      c.execute("INSERT INTO punch_corrections (punch_id, action) VALUES (%s, 'amend')", (punch_id,))
    c.execute("INSERT INTO punch_corrections (punch_id, action, new_kind) VALUES (%s, 'amend', 'clock_out')", (punch_id,))


def test_punch_log_shows_current_name_and_latest_correction(fresh_db: str):
  with psycopg.connect(fresh_db, autocommit=True) as c:
    employee_id = _add_employee(c)
    punch_id = _add_punch(c, employee_id)
    c.execute(
      "INSERT INTO punch_corrections (punch_id, action, recorded_at) VALUES (%s, 'void', now() - interval '1 hour')", (punch_id,)
    )
    c.execute("INSERT INTO punch_corrections (punch_id, action, new_kind) VALUES (%s, 'amend', 'clock_out')", (punch_id,))
    c.execute("UPDATE employees SET name = 'Ann B' WHERE id = %s", (employee_id,))
    row = c.execute("SELECT employee_name, correction, db_login FROM punch_log WHERE id = %s", (punch_id,)).fetchone()
  assert row == ("Ann B", "amend", "postgres")


@pytest.mark.parametrize(
  "statement",
  [
    "INSERT INTO config.business_hours (weekday, opens, closes) VALUES (1, '09:00', '24:00')",
    "UPDATE config.settings SET auto_clock_out_run_time = '24:00'",
    "UPDATE config.settings SET auto_clock_out_stamp_time = '24:00'",
  ],
)
def test_time_columns_reject_24_00(fresh_db: str, statement: LiteralString):
  with psycopg.connect(fresh_db, autocommit=True) as c, pytest.raises(errors.CheckViolation):
    c.execute(statement)


@pytest.mark.parametrize("params", ["null", "[]", "5", '"x"'])
def test_rule_params_must_be_an_object(fresh_db: str, params: str):
  with psycopg.connect(fresh_db, autocommit=True) as c, pytest.raises(errors.CheckViolation):
    c.execute("INSERT INTO rules (kind, params, group_id, on_violation) VALUES ('k', %s::jsonb, 1, 'warn')", (params,))


def test_pin_hash_must_be_bcrypt(fresh_db: str):
  with psycopg.connect(fresh_db, autocommit=True) as c:
    _add_employee(c)  # crypt('1234', gen_salt('bf'))
    with pytest.raises(errors.CheckViolation):
      c.execute("INSERT INTO employees (name, pin_username, pin_hash) VALUES ('Bob', '34', '1234')")


def test_pin_hash_check_survives_nonstandard_strings(admin_dsn: str):
  # With standard_conforming_strings off, a backslash in the CHECK's literal is an escape, and the stored pattern changes.
  name = f"tcs_{uuid4().hex}"
  with psycopg.connect(admin_dsn, autocommit=True) as admin:
    admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
  try:
    with psycopg.connect(make_conninfo(admin_dsn, dbname=name), autocommit=True) as c:
      c.execute("SET standard_conforming_strings = off")
      for m in load_migrations():
        with c.transaction():
          c.execute(cast("LiteralString", m.sql))  # package data, not user input
      _add_employee(c)  # crypt('1234', gen_salt('bf'))
      with pytest.raises(errors.CheckViolation):
        c.execute("INSERT INTO employees (name, pin_username, pin_hash) VALUES ('Bob', '34', '1234')")
  finally:
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
      admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


def test_every_table_loads_into_its_model(fresh_db: str):
  with psycopg.connect(fresh_db, autocommit=True) as c:
    c.execute("INSERT INTO groups (name) VALUES ('night')")
    employee_id, manager_id = _add_employee(c), _add_employee(c, "Bob", "34")
    punch_id = _add_punch(c, employee_id)
    c.execute(
      "INSERT INTO fingerprints (id, employee_id, template, registered_at, instance_id) VALUES (%s, %s, %s, %s, '1')",
      (uuid4(), employee_id, b"tmpl", T),
    )
    c.execute("INSERT INTO punch_corrections (punch_id, action, new_at) VALUES (%s, 'amend', %s)", (punch_id, T))
    c.execute(
      "INSERT INTO overrides (id, punch_id, employee_id, manager_id, reason, pin_only, at, instance_id)"
      " VALUES (%s, %s, %s, %s, 'fingerprint_skipped', true, %s, '1')",
      (uuid4(), punch_id, employee_id, manager_id, T),
    )
    c.execute("""INSERT INTO rules (kind, params, employee_id, on_violation) VALUES ('k', '{"n": 1}', %s, 'refuse')""", (employee_id,))
    c.execute("INSERT INTO config.business_hours (weekday, opens, closes) VALUES (7, '09:00', '23:59:59')")
    c.execute("INSERT INTO backend.environment (tz) VALUES ('UTC')")
    c.execute(
      "INSERT INTO backend.auto_clock_out_runs (scheduled_for, scheduled_tz, stamp, ran_at, closed) VALUES (%s, 'UTC', %s, %s, 2)",
      (T, T, T),
    )
    tables: list[tuple[LiteralString, type[TimeclockModel]]] = [
      ("groups", Group),
      ("employees", Employee),
      ("fingerprints", Fingerprint),
      ("punches", Punch),
      ("punch_corrections", PunchCorrection),
      ("overrides", Override),
      ("rules", RuleRow),
      ("config.business_hours", BusinessHours),
      ("config.settings", Settings),
      ("backend.auto_clock_out_runs", AutoClockOutRun),
    ]
    with c.cursor(row_factory=dict_row) as cur:
      loaded = {table: [model.model_validate(row) for row in cur.execute(f"SELECT * FROM {table}")] for table, model in tables}
  assert {table: len(rows) for table, rows in loaded.items()} == {
    "groups": 2,
    "employees": 2,
    "fingerprints": 1,
    "punches": 1,
    "punch_corrections": 1,
    "overrides": 1,
    "rules": 1,
    "config.business_hours": 1,
    "config.settings": 1,
    "backend.auto_clock_out_runs": 1,
  }
  assert loaded["config.business_hours"][0] == BusinessHours(id=1, weekday=7, opens=time(9), closes=time(23, 59, 59))
  assert loaded["rules"][0].model_dump()["params"] == {"n": 1}
  assert loaded["fingerprints"][0].model_dump()["template"].get_secret_value() == b"tmpl"


@pytest.mark.parametrize(
  ("wall", "zone", "expected"),
  [
    ("2026-10-27 23:00", "America/New_York", datetime(2026, 10, 28, 3, 0, tzinfo=UTC)),  # an ordinary day
    ("2026-03-08 02:30", "America/New_York", datetime(2026, 3, 8, 7, 0, tzinfo=UTC)),  # skipped: runs at 03:00 EDT
    ("2026-03-08 02:00", "America/New_York", datetime(2026, 3, 8, 7, 0, tzinfo=UTC)),
    ("2026-03-08 02:59:59", "America/New_York", datetime(2026, 3, 8, 7, 0, tzinfo=UTC)),
    ("2026-03-08 01:59:59", "America/New_York", datetime(2026, 3, 8, 6, 59, 59, tzinfo=UTC)),
    ("2026-03-08 03:00", "America/New_York", datetime(2026, 3, 8, 7, 0, tzinfo=UTC)),
    ("2026-03-08 23:00", "America/New_York", datetime(2026, 3, 9, 3, 0, tzinfo=UTC)),
    ("2026-11-01 01:30", "America/New_York", datetime(2026, 11, 1, 5, 30, tzinfo=UTC)),  # repeated: the first (EDT)
    ("2026-11-01 01:00", "America/New_York", datetime(2026, 11, 1, 5, 0, tzinfo=UTC)),
    ("2026-11-01 00:59", "America/New_York", datetime(2026, 11, 1, 4, 59, tzinfo=UTC)),
    ("2026-11-01 02:00", "America/New_York", datetime(2026, 11, 1, 7, 0, tzinfo=UTC)),
    ("2026-11-01 23:00", "America/New_York", datetime(2026, 11, 2, 4, 0, tzinfo=UTC)),
    ("2026-10-04 02:15", "Australia/Lord_Howe", datetime(2026, 10, 3, 15, 30, tzinfo=UTC)),  # a 30-minute gap
    ("2026-04-05 01:45", "Australia/Lord_Howe", datetime(2026, 4, 4, 14, 45, tzinfo=UTC)),
    ("2026-10-27 23:00", "UTC", datetime(2026, 10, 27, 23, 0, tzinfo=UTC)),
  ],
)
def test_resolve_local_time(fresh_db: str, wall: str, zone: str, expected: datetime):
  with psycopg.connect(fresh_db) as c:
    c.execute("SET TIME ZONE 'Asia/Tokyo'")  # the session's zone must not matter
    assert c.execute("SELECT backend.resolve_local_time(%s::timestamp, %s)", (wall, zone)).fetchone() == (expected,)


def test_run_row_checks(fresh_db: str):
  with psycopg.connect(fresh_db, autocommit=True) as c:
    insert = "INSERT INTO backend.auto_clock_out_runs (scheduled_for, scheduled_tz, stamp, ran_at, closed) VALUES (%s, 'UTC', %s, %s, %s)"
    for stamp, ran_at, closed in [(T + timedelta(minutes=1), None, None), (T, T, None), (T, None, 0), (T, T, -1)]:
      with pytest.raises(errors.CheckViolation):
        c.execute(insert, (T, stamp, ran_at, closed))
    c.execute(insert, (T, T, None, None))


def test_clock_login_cannot_reach_the_backend_schema(fresh_db: str, clock_login: str):
  with psycopg.connect(clock_login, autocommit=True) as c:
    for statement in [
      "SELECT * FROM backend.auto_clock_out_runs",
      "SELECT * FROM backend.environment",
      "SELECT backend.resolve_local_time('2026-10-27 23:00', 'UTC')",
    ]:
      with pytest.raises(errors.InsufficientPrivilege):
        c.execute(statement)
    assert c.execute("SELECT auto_clock_out_frequency, lunches_enabled, breaks_enabled FROM config.settings").fetchone() == ("daily", True, True)
