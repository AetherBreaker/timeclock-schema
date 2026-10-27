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
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Annotated, LiteralString, cast, get_args
from uuid import uuid4

# Third party imports
import psycopg
import pytest
from psycopg import errors, sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row
from pydantic import StringConstraints, TypeAdapter, ValidationError

# First party imports
from timeclock_schema.migrations import load_migrations
from timeclock_schema.models import (
  EMAIL_PATTERN,
  PHONE_PATTERN,
  AutoClockOutRun,
  BusinessHours,
  Employee,
  Fingerprint,
  Group,
  FingerprintBypass,
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
      "SELECT auto_clock_out_enabled, auto_clock_out_run_time, auto_clock_out_stamp_time, auto_clock_out_frequency,"
      " lunches_enabled, breaks_enabled, paid_break_minutes, weekly_hours_alert FROM config.settings"
    ).fetchall() == [(True, time(23), time(23), "daily", True, True, 30, Decimal(40))]
    assert c.execute("SELECT count(*) FROM config.business_hours").fetchone() == (0,)
    with pytest.raises(errors.UndefinedColumn):  # the hours function replaces them (owner, 2026-10-27)
      c.execute("SELECT subtract_lunch FROM config.settings")


def test_employee_defaults_and_updated_at(fresh_db: str):
  with psycopg.connect(fresh_db, autocommit=True) as c:
    with pytest.raises(errors.CheckViolation):
      c.execute("INSERT INTO employees (name, pin_username, pin_hash) VALUES ('Ann', '12a', crypt('1234', gen_salt('bf')))")
    row = c.execute(
      "INSERT INTO employees (name, pin_username, pin_hash) VALUES ('Ann', '12', crypt('1234', gen_salt('bf')))"
      " RETURNING id, group_id, role, active, fingerprint_exempt, phone, email, updated_at"
    ).fetchone()
    assert row is not None
    employee_id, group_id, role, active, exempt, phone, email, t0 = row
    assert (group_id, role, active, exempt, phone, email) == (1, "employee", True, False, None, None)
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
    c.execute("UPDATE config.settings SET breaks_enabled = false")
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
  punch_id, bypass_id, login_id, fingerprint_id = uuid4(), uuid4(), uuid4(), uuid4()
  with psycopg.connect(clock_login, autocommit=True) as c:
    for _ in range(2):
      c.execute(
        "INSERT INTO punches (id, employee_id, kind, at, instance_id) VALUES (%s, %s, 'clock_in', %s, '1') ON CONFLICT (id) DO NOTHING",
        (punch_id, employee_id, T),
      )
      c.execute(
        "INSERT INTO fingerprint_bypasses (id, punch_id, employee_id, manager_id, reason, pin_only, at, instance_id)"
        " VALUES (%s, %s, %s, %s, 'override_no_fingerprint', false, %s, '1') ON CONFLICT (id) DO NOTHING",
        (bypass_id, punch_id, employee_id, manager_id, T),
      )
      c.execute(
        "INSERT INTO fingerprint_bypasses (id, employee_id, reason, at, instance_id)"
        " VALUES (%s, %s, 'admin_pin_login', %s, '1') ON CONFLICT (id) DO NOTHING",
        (login_id, manager_id, T),
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
      ("SELECT * FROM fingerprint_bypasses", False),
      (
        "INSERT INTO employees (id, name, pin_username, pin_hash, fingerprint_exempt)"
        " VALUES (10001, 'Eve', '10001', crypt('1234', gen_salt('bf')), true)",
        False,
      ),
      ("UPDATE employees SET name = 'x'", False),
      ("UPDATE config.settings SET breaks_enabled = false", False),
    ]
    for sql, takes_punch in forbidden:
      with pytest.raises(errors.InsufficientPrivilege):
        c.execute(sql, (uuid4(), employee_id, T) if takes_punch else None)  # pyright: ignore[reportArgumentType]

  with psycopg.connect(fresh_db) as admin:
    assert admin.execute("SELECT count(*), min(db_login) FROM fingerprint_bypasses").fetchone() == (2, role)


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


def test_employee_ids_are_by_default(fresh_db: str):
  # A clock supplies the number it chose (§6.4); a hand-added row still takes the sequence.
  insert = "INSERT INTO employees (id, name, pin_username, pin_hash) VALUES (%s, 'Ann', %s, crypt('1', gen_salt('bf')))"
  with psycopg.connect(fresh_db, autocommit=True) as c:
    c.execute(insert, (10001, "10001"))
    assert _add_employee(c, "Bob", "34") == 1
    with pytest.raises(errors.UniqueViolation):
      c.execute(insert, (10001, "10002"))  # the number is taken
    with pytest.raises(errors.UniqueViolation):
      c.execute(insert, (10002, "34"))  # the username is taken


@pytest.mark.parametrize(
  ("column", "value", "ok"),
  [
    ("phone", "+15551234567", True),
    ("phone", "5551234567", False),
    ("phone", "+1555123456", False),
    ("phone", "+1 555 123 4567", False),
    ("email", "ann@example.com", True),
    ("email", "a@b.co", True),
    ("email", "a@b", False),
    ("email", "ann example@b.co", False),
    ("email", "@b.co", False),
  ],
)
def test_contact_checks(fresh_db: str, column: str, value: str, *, ok: bool):
  insert = sql.SQL("INSERT INTO employees (name, pin_username, pin_hash, {}) VALUES ('Ann', '12', crypt('1', gen_salt('bf')), %s)")
  with psycopg.connect(fresh_db, autocommit=True) as c:
    if ok:
      c.execute(insert.format(sql.Identifier(column)), (value,))
    else:
      with pytest.raises(errors.CheckViolation):
        c.execute(insert.format(sql.Identifier(column)), (value,))


def _matches(pattern: str, sample: str) -> bool:
  try:
    TypeAdapter(Annotated[str, StringConstraints(pattern=pattern)]).validate_python(sample)
  except ValidationError:
    return False
  return True


def test_contact_patterns_match_the_models(fresh_db: str):
  # The models and the SQL checks share one pattern; pydantic and PostgreSQL must read it alike.
  samples = ["+15551234567", "+1555123456", "15551234567", "a@b.co", "a@b", "a b@c.d", "a@b@c.d", "a@.d", "x@y.z\n"]
  with psycopg.connect(fresh_db) as c:
    for pattern in (PHONE_PATTERN, EMAIL_PATTERN):
      for sample in samples:
        got = c.execute("SELECT %s ~ %s", (sample, pattern)).fetchone()
        assert got == (_matches(pattern, sample),), (pattern, sample)


def test_clock_login_adds_employees_and_managers_but_never_admins(fresh_db: str, clock_login: str):
  insert = (
    "INSERT INTO employees (id, name, pin_username, pin_hash, role, group_id, phone, email)"
    " VALUES (%s, 'Ann', %s, crypt('1', gen_salt('bf')), %s, 1, '+15551234567', 'ann@example.com')"
  )
  with psycopg.connect(clock_login, autocommit=True) as c:
    c.execute(insert, (10001, "10001", "employee"))
    c.execute(insert, (10002, "10002", "manager"))
    with pytest.raises(errors.CheckViolation):
      c.execute(insert, (10003, "10003", "admin"))
  with psycopg.connect(fresh_db, autocommit=True) as admin:
    admin.execute(insert, (10003, "10003", "admin"))  # the owner still can
    rows = admin.execute("SELECT id, role, fingerprint_exempt, active FROM employees ORDER BY id").fetchall()
  assert rows == [(10001, "employee", False, True), (10002, "manager", False, True), (10003, "admin", False, True)]


@pytest.mark.parametrize(
  ("columns", "ok"),
  [
    ("punch_id, manager_id, pin_only", True),  # an override
    ("manager_id, pin_only", False),  # no punch
    ("punch_id, pin_only", False),  # no manager
    ("punch_id, manager_id", False),  # no pin_only
  ],
)
def test_override_bypass_checks(fresh_db: str, columns: str, *, ok: bool):
  with psycopg.connect(fresh_db, autocommit=True) as c:
    employee_id, manager_id = _add_employee(c), _add_employee(c, "Bob", "34")
    values = {"punch_id": _add_punch(c, employee_id), "manager_id": manager_id, "pin_only": True}
    names = columns.split(", ")
    statement = sql.SQL(
      "INSERT INTO fingerprint_bypasses (id, employee_id, reason, at, instance_id, {}) VALUES (%s, %s, 'override_no_fingerprint', %s, '1', {})"
    ).format(sql.SQL(", ").join(map(sql.Identifier, names)), sql.SQL(", ").join(sql.Placeholder() * len(names)))
    params = (uuid4(), employee_id, T, *(values[n] for n in names))
    if ok:
      c.execute(statement, params)
    else:
      with pytest.raises(errors.CheckViolation):
        c.execute(statement, params)


@pytest.mark.parametrize("reason", ["admin_pin_login", "exempt_pin_login"])
def test_login_bypass_checks(fresh_db: str, reason: str):
  with psycopg.connect(fresh_db, autocommit=True) as c:
    employee_id, manager_id = _add_employee(c), _add_employee(c, "Bob", "34")
    insert = (
      "INSERT INTO fingerprint_bypasses (id, employee_id, reason, at, instance_id, punch_id, manager_id, pin_only)"
      " VALUES (%s, %s, %s, %s, '1', %s, %s, %s)"
    )
    c.execute(insert, (uuid4(), employee_id, reason, T, None, None, None))  # a login that punched nothing
    c.execute(insert, (uuid4(), employee_id, reason, T, _add_punch(c, employee_id), None, None))  # a PIN rescan's punch
    for manager, pin_only in [(manager_id, None), (None, True)]:
      with pytest.raises(errors.CheckViolation):
        c.execute(insert, (uuid4(), employee_id, reason, T, None, manager, pin_only))


@pytest.mark.parametrize(
  "statement", ["UPDATE config.settings SET paid_break_minutes = -1", "UPDATE config.settings SET weekly_hours_alert = 0"]
)
def test_hours_settings_checks(fresh_db: str, statement: LiteralString):
  with psycopg.connect(fresh_db, autocommit=True) as c, pytest.raises(errors.CheckViolation):
    c.execute(statement)


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
      "INSERT INTO fingerprint_bypasses (id, punch_id, employee_id, manager_id, reason, pin_only, at, instance_id)"
      " VALUES (%s, %s, %s, %s, 'override_fingerprint_skipped', true, %s, '1')",
      (uuid4(), punch_id, employee_id, manager_id, T),
    )
    c.execute(
      "INSERT INTO fingerprint_bypasses (id, employee_id, reason, at, instance_id) VALUES (%s, %s, 'exempt_pin_login', %s, '1')",
      (uuid4(), employee_id, T),
    )
    c.execute("""INSERT INTO rules (kind, params, employee_id, on_violation) VALUES ('k', '{"n": 1}', %s, 'refuse')""", (employee_id,))
    c.execute("INSERT INTO config.business_hours (weekday, opens, closes) VALUES (7, '09:00', '23:59:59')")
    c.execute("INSERT INTO backend.environment (tz) VALUES ('UTC')")
    tables: list[tuple[LiteralString, type[TimeclockModel]]] = [
      ("groups", Group),
      ("employees", Employee),
      ("fingerprints", Fingerprint),
      ("punches", Punch),
      ("punch_corrections", PunchCorrection),
      ("fingerprint_bypasses", FingerprintBypass),
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
    "fingerprint_bypasses": 2,
    "rules": 1,
    "config.business_hours": 1,
    "config.settings": 1,
    "backend.auto_clock_out_runs": 7,
  }
  assert loaded["config.business_hours"][0] == BusinessHours(id=1, weekday=7, opens=time(9), closes=time(23, 59, 59))
  assert loaded["rules"][0].model_dump()["params"] == {"n": 1}
  assert loaded["fingerprints"][0].model_dump()["template"].get_secret_value() == b"tmpl"


def test_run_row_checks(fresh_db: str):
  with psycopg.connect(fresh_db, autocommit=True) as c:
    insert = (
      "INSERT INTO backend.auto_clock_out_runs (scheduled_for, scheduled_tz, stamp, ran_at, closed) VALUES (%s, 'UTC', %s, %s, %s)"
    )
    for ran_at, closed in [(T, None), (None, 0), (T, -1)]:
      with pytest.raises(errors.CheckViolation):
        c.execute(insert, (T, T, ran_at, closed))
    c.execute(insert, (T, T, None, None))
    c.execute(insert, (T, T + timedelta(hours=1), None, None))  # a stamp after the run is allowed (owner, 2026-10-27)


def test_clock_login_cannot_reach_the_backend_schema(fresh_db: str, clock_login: str):
  with psycopg.connect(clock_login, autocommit=True) as c:
    statements: list[LiteralString] = [
      "SELECT * FROM backend.auto_clock_out_runs",
      "SELECT * FROM backend.environment",
      "SELECT backend.project_auto_clock_out_runs()",
    ]
    for statement in statements:
      with pytest.raises(errors.InsufficientPrivilege):
        c.execute(statement)
    assert c.execute("SELECT auto_clock_out_frequency, lunches_enabled, breaks_enabled FROM config.settings").fetchone() == (
      "daily",
      True,
      True,
    )


def _noon_zone() -> str:
  """An Etc zone where it's now 12:xx, so a 23:00 run today is hours ahead whenever the test runs."""
  offset = 12 - datetime.now(UTC).hour  # -11..12; Etc names invert the sign (Etc/GMT-5 is UTC+5)
  return "UTC" if offset == 0 else f"Etc/GMT{-offset:+d}"


def _runs(c: psycopg.Connection) -> list[tuple[datetime, datetime, str, bool]]:
  """Each row's (local run time, local stamp, zone, has run), in time order."""
  return c.execute(
    "SELECT scheduled_for AT TIME ZONE scheduled_tz, stamp AT TIME ZONE scheduled_tz, scheduled_tz, ran_at IS NOT NULL"
    " FROM backend.auto_clock_out_runs ORDER BY scheduled_for"
  ).fetchall()


def _local_today(c: psycopg.Connection, zone: str) -> date:
  row = c.execute("SELECT (now() AT TIME ZONE %s)::date", (zone,)).fetchone()
  assert row is not None
  return row[0]


def _as_of_only(c: psycopg.Connection) -> None:
  """Disable both reprojection triggers, so only explicit `project_auto_clock_out_runs(as_of)` calls project, at fixed times."""
  c.execute("ALTER TABLE config.settings DISABLE TRIGGER settings_reproject_auto_clock_out")
  c.execute("ALTER TABLE backend.environment DISABLE TRIGGER environment_reproject_auto_clock_out")
  c.execute("INSERT INTO backend.environment (tz) VALUES ('America/New_York')")


def _project(c: psycopg.Connection, as_of: str) -> None:
  c.execute("SELECT backend.project_auto_clock_out_runs(%s::timestamptz)", (as_of,))


def _days(start: date, run: time, stamp: time) -> list[tuple[datetime, datetime, str, bool]]:
  """Seven unrun New York rows from `start`."""
  return [
    (datetime.combine(start + timedelta(days=i), run), datetime.combine(start + timedelta(days=i), stamp), "America/New_York", False)
    for i in range(7)
  ]


def test_no_projection_until_the_backend_writes_a_zone(fresh_db: str):
  with psycopg.connect(fresh_db, autocommit=True) as c:
    c.execute("UPDATE config.settings SET breaks_enabled = false")  # no zone: must not fail (the clocks' own test databases)
    assert _runs(c) == []


def test_zone_insert_projects_seven_days(fresh_db: str):
  zone = _noon_zone()
  with psycopg.connect(fresh_db, autocommit=True) as c:
    c.execute("INSERT INTO backend.environment (tz) VALUES (%s)", (zone,))
    today = _local_today(c, zone)
    expected = [
      (datetime.combine(today + timedelta(days=i), time(23)), datetime.combine(today + timedelta(days=i), time(23)), zone, False)
      for i in range(7)
    ]
    assert _runs(c) == expected


def test_todays_passed_run_time_is_still_projected(fresh_db: str):
  with psycopg.connect(fresh_db, autocommit=True) as c:
    _as_of_only(c)
    _project(c, "2026-10-27 23:30-04")
    assert _runs(c) == _days(date(2026, 10, 27), time(23), time(23))


def test_reprojection_never_touches_a_run_row(fresh_db: str):
  with psycopg.connect(fresh_db, autocommit=True) as c:
    _as_of_only(c)
    _project(c, "2026-10-27 22:00-04")
    c.execute(
      "UPDATE backend.auto_clock_out_runs SET ran_at = '2026-10-28 03:00:05Z', closed = 2 WHERE scheduled_for = '2026-10-28 03:00Z'"
    )
    ran = c.execute("SELECT * FROM backend.auto_clock_out_runs WHERE ran_at IS NOT NULL").fetchall()
    c.execute("UPDATE config.settings SET auto_clock_out_run_time = '22:30', auto_clock_out_stamp_time = '22:00'")
    _project(c, "2026-10-27 23:40-04")
    assert c.execute("SELECT * FROM backend.auto_clock_out_runs WHERE ran_at IS NOT NULL").fetchall() == ran
    rows = _runs(c)
    assert rows[0] == (datetime(2026, 10, 27, 23), datetime(2026, 10, 27, 23), "America/New_York", True)  # noqa: DTZ001 (local wall times)
    assert rows[1:] == _days(date(2026, 10, 28), time(22, 30), time(22))[:6]


def test_run_time_moved_into_the_past_projects_today_anyway(fresh_db: str):
  with psycopg.connect(fresh_db, autocommit=True) as c:
    _as_of_only(c)
    _project(c, "2026-10-27 20:00-04")
    c.execute("UPDATE config.settings SET auto_clock_out_run_time = '19:00', auto_clock_out_stamp_time = '18:00'")
    _project(c, "2026-10-27 20:10-04")
    assert _runs(c) == _days(date(2026, 10, 27), time(19), time(18))


def test_missed_row_is_kept_and_today_not_duplicated(fresh_db: str):
  with psycopg.connect(fresh_db, autocommit=True) as c:
    _as_of_only(c)
    _project(c, "2026-10-27 20:00-04")
    missed = c.execute("SELECT * FROM backend.auto_clock_out_runs ORDER BY scheduled_for LIMIT 1").fetchone()
    c.execute("UPDATE config.settings SET auto_clock_out_run_time = '23:30', auto_clock_out_stamp_time = '23:30'")
    _project(c, "2026-10-27 23:10-04")  # 23:00 passed unrun (the backend was down): history, caught up with its own stamp
    assert c.execute("SELECT * FROM backend.auto_clock_out_runs ORDER BY scheduled_for LIMIT 1").fetchone() == missed
    assert _runs(c)[1:] == _days(date(2026, 10, 28), time(23, 30), time(23, 30))[:6]


def test_dst_weeks_use_postgres_resolution(fresh_db: str):
  # Owner, 2026-10-27: Postgres's own AT TIME ZONE. Skipped times move forward by the gap, repeated ones take the second.
  with psycopg.connect(fresh_db, autocommit=True) as c:
    c.execute("SET TIME ZONE 'Asia/Tokyo'")  # the session's zone must not matter
    _as_of_only(c)
    c.execute("UPDATE config.settings SET auto_clock_out_run_time = '02:30', auto_clock_out_stamp_time = '02:15'")
    _project(c, "2026-03-06 12:00-05")
    spring = c.execute("SELECT scheduled_for, stamp FROM backend.auto_clock_out_runs ORDER BY scheduled_for LIMIT 4").fetchall()
    assert spring == [
      (datetime(2026, 3, 6, 7, 30, tzinfo=UTC), datetime(2026, 3, 6, 7, 15, tzinfo=UTC)),
      (datetime(2026, 3, 7, 7, 30, tzinfo=UTC), datetime(2026, 3, 7, 7, 15, tzinfo=UTC)),
      (datetime(2026, 3, 8, 7, 30, tzinfo=UTC), datetime(2026, 3, 8, 7, 15, tzinfo=UTC)),  # both skipped: 03:30/03:15 EDT
      (datetime(2026, 3, 9, 6, 30, tzinfo=UTC), datetime(2026, 3, 9, 6, 15, tzinfo=UTC)),
    ]
    c.execute("DELETE FROM backend.auto_clock_out_runs")
    # Only the stamp is skipped, and it moves past the run time: stored as resolved (03:30 EDT, after the 03:00 run).
    c.execute("UPDATE config.settings SET auto_clock_out_run_time = '03:00', auto_clock_out_stamp_time = '02:30'")
    _project(c, "2026-03-06 12:00-05")
    assert c.execute(
      "SELECT scheduled_for, stamp FROM backend.auto_clock_out_runs WHERE (scheduled_for AT TIME ZONE scheduled_tz)::date = '2026-03-08'"
    ).fetchall() == [(datetime(2026, 3, 8, 7, 0, tzinfo=UTC), datetime(2026, 3, 8, 7, 30, tzinfo=UTC))]
    c.execute("DELETE FROM backend.auto_clock_out_runs")
    c.execute("UPDATE config.settings SET auto_clock_out_run_time = '01:30', auto_clock_out_stamp_time = '01:00'")
    _project(c, "2026-10-30 12:00-04")
    fall = c.execute(
      "SELECT scheduled_for, stamp FROM backend.auto_clock_out_runs WHERE (scheduled_for AT TIME ZONE scheduled_tz)::date = '2026-11-01'"
    ).fetchall()
    assert fall == [(datetime(2026, 11, 1, 6, 30, tzinfo=UTC), datetime(2026, 11, 1, 6, 0, tzinfo=UTC))]  # second occurrences (EST)


def test_disabling_prunes_the_future_and_keeps_history(fresh_db: str):
  zone = _noon_zone()
  with psycopg.connect(fresh_db, autocommit=True) as c:
    c.execute("INSERT INTO backend.environment (tz) VALUES (%s)", (zone,))
    c.execute(
      "INSERT INTO backend.auto_clock_out_runs (scheduled_for, scheduled_tz, stamp, ran_at, closed) VALUES"
      " (now() - interval '2 days', %s, now() - interval '50 hours', NULL, NULL),"
      " (now() - interval '1 day', %s, now() - interval '25 hours', now() - interval '1 day', 0)",
      (zone, zone),
    )
    history = c.execute("SELECT * FROM backend.auto_clock_out_runs WHERE scheduled_for < now() ORDER BY id").fetchall()
    c.execute("UPDATE config.settings SET auto_clock_out_enabled = false")
    assert c.execute("SELECT * FROM backend.auto_clock_out_runs ORDER BY id").fetchall() == history
    c.execute("UPDATE config.settings SET auto_clock_out_enabled = true")
    assert c.execute("SELECT count(*) FROM backend.auto_clock_out_runs WHERE scheduled_for > now()").fetchone() == (7,)
    assert c.execute("SELECT * FROM backend.auto_clock_out_runs WHERE scheduled_for < now() ORDER BY id").fetchall() == history


def test_deleted_settings_prune_and_project_nothing(fresh_db: str):
  with psycopg.connect(fresh_db, autocommit=True) as c:
    c.execute("INSERT INTO backend.environment (tz) VALUES (%s)", (_noon_zone(),))
    c.execute("DELETE FROM config.settings")
    assert _runs(c) == []


def test_stamp_after_the_run_time_is_projected_as_given(fresh_db: str):
  # No stamp <= run rule (owner, 2026-10-27, superseding R41.3): each day's stamp is that day's stamp time.
  with psycopg.connect(fresh_db, autocommit=True) as c:
    _as_of_only(c)
    c.execute("UPDATE config.settings SET auto_clock_out_run_time = '22:00', auto_clock_out_stamp_time = '23:00'")
    _project(c, "2026-10-27 12:00-04")
    assert _runs(c) == _days(date(2026, 10, 27), time(22), time(23))


def test_zone_change_reprojects_and_keeps_run_rows(fresh_db: str):
  with psycopg.connect(fresh_db, autocommit=True) as c:
    c.execute("INSERT INTO backend.environment (tz) VALUES (%s)", (_noon_zone(),))
    c.execute(
      "INSERT INTO backend.auto_clock_out_runs (scheduled_for, scheduled_tz, stamp, ran_at, closed)"
      # 3 days back: a day-old New York date can be Chicago's today just after midnight, and would then hold today.
      " VALUES (now() - interval '3 days', 'America/New_York', now() - interval '3 days', now() - interval '3 days', 1)"
    )
    ran = c.execute("SELECT * FROM backend.auto_clock_out_runs WHERE ran_at IS NOT NULL").fetchall()
    c.execute("UPDATE backend.environment SET tz = 'America/Chicago'")
    assert c.execute("SELECT * FROM backend.auto_clock_out_runs WHERE ran_at IS NOT NULL").fetchall() == ran
    unrun = c.execute(
      "SELECT DISTINCT scheduled_tz, count(*) OVER () FROM backend.auto_clock_out_runs WHERE ran_at IS NULL"
    ).fetchall()
    assert unrun == [("America/Chicago", 7)]


def test_unrelated_settings_edit_reprojects_identically(fresh_db: str):
  with psycopg.connect(fresh_db, autocommit=True) as c:
    c.execute("INSERT INTO backend.environment (tz) VALUES (%s)", (_noon_zone(),))
    before = _runs(c)
    c.execute("UPDATE config.settings SET breaks_enabled = false")
    assert _runs(c) == before
