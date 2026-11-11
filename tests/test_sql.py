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
from zoneinfo import ZoneInfo

# Third party imports
import psycopg
import pytest
from psycopg import errors, sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row
from pydantic import StringConstraints, TypeAdapter, ValidationError

# First party imports
from timeclock_schema.enums import Finger
from timeclock_schema.migrations import load_migrations
from timeclock_schema.models import (
  EMAIL_PATTERN,
  PHONE_PATTERN,
  AutoClockOutRun,
  BusinessHours,
  Edit,
  Editor,
  Employee,
  EmployeeChange,
  Fingerprint,
  FingerprintBypass,
  FingerprintChange,
  Group,
  Punch,
  PunchCorrection,
  RuleRow,
  ScheduledRun,
  Settings,
)
from timeclock_schema.shifts import apply_corrections, build_timeline, total_hours, week_bounds
from timeclock_schema.wire import NOTIFY_CHANNEL, Notification, NotifyTable

if TYPE_CHECKING:
  # Standard library imports
  from collections.abc import Callable, Generator, Iterator

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


@pytest.fixture
def web_login(admin_dsn: str, fresh_db: str) -> str:
  # The migration creates timeclock_web NOLOGIN; the backend gives it LOGIN and a password. Server-wide, so left as is.
  with psycopg.connect(admin_dsn, autocommit=True) as admin:
    admin.execute("ALTER ROLE timeclock_web LOGIN PASSWORD 'pw'")
  return make_conninfo(fresh_db, user="timeclock_web", password="pw")


def _new_email() -> str:
  return f"ed-{uuid4().hex[:12]}@example.com"


@pytest.fixture
def make_editor(admin_dsn: str, fresh_db: str) -> Iterator[Callable[..., str]]:
  """Adds `backend.editors` rows; badges are server-wide and outlive the dropped database, so teardown drops them."""
  emails: list[str] = []

  def make(employee_id: int | None = None, *, developer: bool = False) -> str:
    email = _new_email()
    with psycopg.connect(fresh_db, autocommit=True) as c:
      c.execute("INSERT INTO backend.editors (email, employee_id, developer) VALUES (%s, %s, %s)", (email, employee_id, developer))
    emails.append(email)
    return email

  try:
    yield make
  finally:
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
      for email in emails:
        admin.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(email)))


def _role_exists(c: psycopg.Connection, role: str) -> bool:
  row = c.execute("SELECT count(*) FROM pg_roles WHERE rolname = %s", (role,)).fetchone()
  assert row is not None
  return row[0] == 1


def _add_employee(c: psycopg.Connection, first_name: str = "Ann", pin_username: str = "12") -> int:
  row = c.execute(
    "INSERT INTO employees (first_name, last_name, pin_username, pin_hash)"
    " VALUES (%s, 'Lee', %s, crypt('1234', gen_salt('bf'))) RETURNING id",
    (first_name, pin_username),
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
      c.execute(
        "INSERT INTO employees (first_name, last_name, pin_username, pin_hash) VALUES ('Ann', 'Lee', '12a', crypt('1234', gen_salt('bf')))"
      )
    row = c.execute(
      "INSERT INTO employees (first_name, last_name, pin_username, pin_hash) VALUES ('Ann', 'Lee', '12', crypt('1234', gen_salt('bf')))"
      " RETURNING id, group_id, role, active, fingerprint_exempt, phone, email, updated_at"
    ).fetchone()
    assert row is not None
    employee_id, group_id, role, active, exempt, phone, email, t0 = row
    assert (group_id, role, active, exempt, phone, email) == (1, "employee", True, False, None, None)
    assert c.execute("SELECT crypt('1234', pin_hash) = pin_hash FROM employees WHERE id = %s", (employee_id,)).fetchone() == (True,)
    updated = c.execute("UPDATE employees SET first_name = 'Annie' WHERE id = %s RETURNING updated_at", (employee_id,)).fetchone()
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
    c.execute("UPDATE employees SET first_name = 'Annie' WHERE id = %s", (employee_id,))
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
        "INSERT INTO fingerprints (id, employee_id, finger, template, registered_at, instance_id)"
        " VALUES (%s, %s, 'left_index', %s, %s, '1') ON CONFLICT (id) DO NOTHING",
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
        (
          "INSERT INTO employees (id, first_name, last_name, pin_username, pin_hash, fingerprint_exempt)"
          " VALUES (10001, 'Eve', 'Lee', '10001', crypt('1234', gen_salt('bf')), true)"
        ),
        False,
      ),
      ("UPDATE employees SET first_name = 'x'", False),
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


def test_owner_adds_punches_without_an_id(fresh_db: str):
  with psycopg.connect(fresh_db, autocommit=True) as c:
    employee_id = _add_employee(c)
    ids = c.execute(
      "INSERT INTO punches (employee_id, kind, at, instance_id) VALUES (%(e)s, 'clock_in', %(at)s, 'manual'),"
      " (%(e)s, 'clock_out', %(at)s, 'manual') RETURNING id",
      {"e": employee_id, "at": T},
    ).fetchall()
  (first,), (second,) = ids
  assert first != second


def test_correction_check_constraint(fresh_db: str):
  with psycopg.connect(fresh_db, autocommit=True) as c:
    punch_id = _add_punch(c, _add_employee(c))
    with pytest.raises(errors.CheckViolation):
      c.execute("INSERT INTO punch_corrections (punch_id, action, new_at) VALUES (%s, 'void', %s)", (punch_id, T))
    with pytest.raises(errors.CheckViolation):
      c.execute("INSERT INTO punch_corrections (punch_id, action) VALUES (%s, 'amend')", (punch_id,))
    c.execute("INSERT INTO punch_corrections (punch_id, action, new_kind) VALUES (%s, 'amend', 'clock_out')", (punch_id,))


def _add_punch_at(c: psycopg.Connection, employee_id: int, kind: str, at: datetime) -> str:
  punch_id = str(uuid4())
  c.execute(
    "INSERT INTO punches (id, employee_id, kind, at, instance_id) VALUES (%s, %s, %s, %s, '1')", (punch_id, employee_id, kind, at)
  )
  return punch_id


def test_punch_log_shows_punches_as_corrected_in_corrected_order(fresh_db: str):
  # The export reads this view: the newest correction's time and type stand in for the original, and order follows them.
  hour = timedelta(hours=1)
  with psycopg.connect(fresh_db, autocommit=True) as c:
    employee_id = _add_employee(c)
    first = _add_punch_at(c, employee_id, "clock_in", T)
    second = _add_punch_at(c, employee_id, "clock_out", T + hour)
    c.execute(
      "INSERT INTO punch_corrections (punch_id, action, new_at, new_kind) VALUES (%s, 'amend', %s, 'clock_in')", (second, T - hour)
    )
    # A void, then a newer amend: the amend wins and brings the punch back, with the type it sets and its original time.
    c.execute("INSERT INTO punch_corrections (punch_id, action, recorded_at) VALUES (%s, 'void', now() - interval '1 hour')", (first,))
    c.execute("INSERT INTO punch_corrections (punch_id, action, new_kind, note) VALUES (%s, 'amend', 'break_start', 'x')", (first,))
    c.execute("UPDATE employees SET middle_name = 'B' WHERE id = %s", (employee_id,))
    rows = c.execute("SELECT id::text, employee_name, kind::text, at, correction_note, db_login FROM punch_log").fetchall()
  assert rows == [
    (second, "Ann B Lee", "clock_in", T - hour, "", "postgres"),
    (first, "Ann B Lee", "break_start", T, "x", "postgres"),
  ]


def test_punch_log_leaves_out_voided_punches(fresh_db: str):
  with psycopg.connect(fresh_db, autocommit=True) as c:
    employee_id = _add_employee(c)
    kept, voided = _add_punch_at(c, employee_id, "clock_in", T), _add_punch_at(c, employee_id, "clock_out", T)
    c.execute("INSERT INTO punch_corrections (punch_id, action) VALUES (%s, 'void')", (voided,))
    ids = [r[0] for r in c.execute("SELECT id::text FROM punch_log").fetchall()]
  assert ids == [kept]


def test_punch_log_has_no_correction_columns(fresh_db: str):
  # Only the corrected values: the correction's action and new values aren't columns (owner, 2026-11-02).
  with psycopg.connect(fresh_db, autocommit=True) as c:
    columns = {r[0] for r in c.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'punch_log'")}
  assert {"kind", "at", "correction_note", "corrected_at"} <= columns
  assert not columns & {"correction", "new_at", "new_kind"}


NY = ZoneInfo("America/New_York")


def _ny(day: int, hour: int, minute: int = 0, second: int = 0, month: int = 10) -> datetime:
  return datetime(2026, month, day, hour, minute, second, tzinfo=NY)


def _hm(td: timedelta) -> str:
  seconds = int(td.total_seconds())
  return f"{seconds // 3600}:{seconds // 60 % 60:02d}"


def test_weekly_hours_formats_one_row_per_employee_for_the_week(fresh_db: str):
  # The payroll export: one week's rows by its Monday. Ann's Monday: 8:05:59 in, two 20-minute breaks (30 paid, 10
  # unpaid), a 30-minute lunch, so 6:55:59 worked; Tuesday 8:00 more.
  with psycopg.connect(fresh_db, autocommit=True) as c:
    c.execute("INSERT INTO backend.environment (tz) VALUES ('America/New_York')")
    ann, bob = _add_employee(c), _add_employee(c, "Bob", "34")
    for kind, at in [
      ("clock_in", _ny(19, 9)),
      ("break_start", _ny(19, 10)),
      ("break_end", _ny(19, 10, 20)),
      ("lunch_start", _ny(19, 12)),
      ("lunch_end", _ny(19, 12, 30)),
      ("break_start", _ny(19, 15)),
      ("break_end", _ny(19, 15, 20)),
      ("clock_out", _ny(19, 17, 5, 59)),
      ("clock_in", _ny(20, 9)),
      ("clock_out", _ny(20, 17)),
      ("clock_in", _ny(26, 9)),  # the next week
      ("clock_out", _ny(26, 10)),
    ]:
      _add_punch_at(c, ann, kind, at)
    _add_punch_at(c, bob, "clock_in", _ny(21, 9))
    _add_punch_at(c, bob, "clock_out", _ny(21, 9, 5))
    rows = c.execute(
      "SELECT employee_name, hours, breaks, lunches, unpaid_breaks FROM weekly_hours WHERE week_of = '2026-10-19'"
    ).fetchall()
    columns = [
      r[0]
      for r in c.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'weekly_hours' ORDER BY ordinal_position"
      )
    ]
    days = c.execute(
      "SELECT day, hours, breaks, lunches, unpaid_breaks FROM daily_hours WHERE employee_id = %s AND day < '2026-10-26'", (ann,)
    ).fetchall()
  assert rows == [("Ann Lee", "14:55", "0:30", "0:30", "0:10"), ("Bob Lee", "0:05", "0:00", "0:00", "0:00")]
  assert columns == ["employee_id", "employee_name", "week_of", "hours", "breaks", "lunches", "unpaid_breaks"]
  assert days == [(date(2026, 10, 20), "8:00", "0:00", "0:00", "0:00"), (date(2026, 10, 19), "6:55", "0:30", "0:30", "0:10")]


def _day_bounds(at: datetime, tz: ZoneInfo) -> tuple[datetime, datetime]:
  day = at.astimezone(tz).date()
  return datetime.combine(day, time(0), tzinfo=tz).astimezone(UTC), datetime.combine(
    day + timedelta(days=1), time(0), tzinfo=tz
  ).astimezone(UTC)


def test_hours_views_match_total_hours(fresh_db: str):
  # The views repeat total_hours's rules; each week's and day's totals must be the same. Covered: the daily allowance spent
  # across two breaks, and across two shifts' breaks capped at the default 15-minute limit, lunch, a repeated clock-in and a
  # stray break end, a void and an amend, a shift and a break across Monday 00:00, a shift across the end of DST, and a
  # shift still open, on a break, with an automatic clock-out stamped later.
  with psycopg.connect(fresh_db, autocommit=True) as c:
    c.execute("INSERT INTO backend.environment (tz) VALUES ('America/New_York')")
    ann, bob = _add_employee(c), _add_employee(c, "Bob", "34")
    for kind, at in [
      ("clock_in", _ny(20, 9)),
      ("break_start", _ny(20, 10)),
      ("break_end", _ny(20, 10, 20)),
      ("lunch_start", _ny(20, 12)),
      ("lunch_end", _ny(20, 12, 45)),
      ("break_start", _ny(20, 13)),
      ("break_end", _ny(20, 13, 20)),
      ("clock_out", _ny(20, 17)),
      ("clock_in", _ny(21, 9)),
      ("clock_in", _ny(21, 9, 5)),
      ("break_end", _ny(21, 9, 30)),
      ("clock_in", _ny(22, 9)),  # 20 minutes pays 15 and leaves 15 of the day's 30 for the afternoon's 12
      ("break_start", _ny(22, 10)),
      ("break_end", _ny(22, 10, 20)),
      ("clock_out", _ny(22, 12)),
      ("clock_in", _ny(22, 13)),
      ("break_start", _ny(22, 14)),
      ("break_end", _ny(22, 14, 12)),
      ("clock_out", _ny(22, 17)),
      ("clock_in", _ny(25, 22)),  # Sunday into Monday
      ("break_start", _ny(25, 23, 50)),
      ("break_end", _ny(26, 0, 20)),
      ("clock_out", _ny(26, 2)),
      ("clock_in", datetime(2026, 11, 1, 4, 30, tzinfo=UTC)),  # 00:30 EDT to 03:30 EST: 4 hours
      ("clock_out", datetime(2026, 11, 1, 8, 30, tzinfo=UTC)),
    ]:
      _add_punch_at(c, ann, kind, at)
    amended = _add_punch_at(c, ann, "clock_out", _ny(21, 17))
    c.execute("INSERT INTO punch_corrections (punch_id, action, new_at) VALUES (%s, 'amend', %s)", (amended, _ny(21, 17, 30)))
    voided = _add_punch_at(c, ann, "break_start", _ny(21, 11))
    c.execute("INSERT INTO punch_corrections (punch_id, action) VALUES (%s, 'void')", (voided,))
    row = c.execute("SELECT now()").fetchone()
    assert row is not None
    for kind, ago in [("clock_in", 3), ("lunch_start", 2.5), ("lunch_end", 2), ("break_start", 0.75)]:
      _add_punch_at(c, bob, kind, row[0] - timedelta(hours=ago))
    c.execute(
      "INSERT INTO punches (id, employee_id, kind, at, instance_id, automatic) VALUES (%s, %s, 'clock_out', %s, '1', true)",
      (uuid4(), bob, row[0] + timedelta(hours=1)),
    )
    with c.transaction():  # one now() for the view and the expectation
      row = c.execute("SELECT now()").fetchone()
      assert row is not None
      now = row[0]
      got = {
        view: {
          (r[0], r[1]): r[2:]
          for r in c.execute(
            sql.SQL("SELECT employee_id, {}, hours, breaks, lunches, unpaid_breaks FROM {}").format(
              sql.Identifier(column), sql.Identifier(view)
            )
          )
        }
        for view, column in (("weekly_hours", "week_of"), ("daily_hours", "day"))
      }
      got["shift_summary"] = {
        (r[0], r[1]): r[2:]
        for r in c.execute("SELECT employee_id, clock_in, hours, breaks, lunches, unpaid_breaks FROM shift_summary")
      }
    log = c.execute("SELECT employee_id, id, kind::text, at FROM punch_log").fetchall()
    with c.cursor(row_factory=dict_row) as cur:
      settings = Settings.model_validate(cur.execute("SELECT * FROM config.settings").fetchone())
      all_paid = settings.model_copy(update={"paid_break_minutes": 24 * 60, "break_limit_minutes": 0})
      expected = {"weekly_hours": {}, "daily_hours": {}, "shift_summary": {}}
      expected_log = []
      left_out = []
      for employee_id in (ann, bob):
        punches = [Punch.model_validate(r) for r in cur.execute("SELECT * FROM punches WHERE employee_id = %s", (employee_id,))]
        corrections = [
          PunchCorrection.model_validate(r)
          for r in cur.execute(
            "SELECT pc.* FROM punch_corrections pc JOIN punches p ON p.id = pc.punch_id WHERE p.employee_id = %s", (employee_id,)
          )
        ]
        # punch_log holds the punches the timeline counts: every corrected one but its anomalies.
        anomalies = build_timeline(punches, corrections).anomalies
        ignored = {a.punch.id for a in anomalies}
        left_out += [a.punch.at for a in anomalies]
        effective = apply_corrections(punches, corrections)
        expected_log += [(employee_id, e.id, e.kind.value, e.at) for e in effective if e.id not in ignored]
        for view, bounds in (("weekly_hours", week_bounds), ("daily_hours", _day_bounds)):
          start, end = bounds(min(p.at for p in punches), NY)
          while start < now:
            t = total_hours(punches, corrections, settings=settings, tz=NY, start=start, end=end, now=now)
            # total_hours's unpaid is lunch plus unpaid break; with every break paid, it's lunch alone.
            lunch = total_hours(punches, corrections, settings=all_paid, tz=NY, start=start, end=end, now=now).unpaid
            if t.worked + t.paid_break + t.unpaid > timedelta():  # any shift time in the period
              expected[view][employee_id, start.astimezone(NY).date()] = (
                _hm(t.worked),
                _hm(t.paid_break),
                _hm(lunch),
                _hm(t.unpaid - lunch),
              )
            start, end = bounds(end, NY)
        # A shift's totals are total_hours over its own span, which no other shift overlaps.
        for shift in build_timeline(punches, corrections, as_of=now).shifts:
          start, end = shift.clock_in.at, now if shift.clock_out is None else shift.clock_out.at
          t = total_hours(punches, corrections, settings=settings, tz=NY, start=start, end=end, now=now)
          lunch = total_hours(punches, corrections, settings=all_paid, tz=NY, start=start, end=end, now=now).unpaid
          expected["shift_summary"][employee_id, start] = (_hm(t.worked), _hm(t.paid_break), _hm(lunch), _hm(t.unpaid - lunch))
  assert got == expected
  assert (ann, date(2026, 10, 19)) in got["weekly_hours"] and (ann, date(2026, 10, 26)) in got["weekly_hours"]
  assert {(ann, date(2026, 10, 25)), (ann, date(2026, 10, 26)), (ann, date(2026, 11, 1))} <= got["daily_hours"].keys()
  assert sorted(log, key=lambda r: r[0]) == expected_log  # a stable sort keeps each employee's log in time order
  assert left_out == [_ny(21, 9, 5), _ny(21, 9, 30)]  # Ann's repeated clock-in and stray break end


def test_shift_summary_breaks_down_each_shift(fresh_db: str, clock_login: str):
  # Monday, from the warehouse clock: a 17:30 break (+2 over 15), a lunch, a 15:59 break (not over), and the backend's
  # automatic clock-out; 33:29 of break, 30:00 paid. Tuesday: a clock-out the owner amended from 9:30 to 10:00.
  with psycopg.connect(fresh_db, autocommit=True) as c:
    c.execute("INSERT INTO backend.environment (tz) VALUES ('America/New_York')")
    ann = _add_employee(c)
  with psycopg.connect(clock_login, autocommit=True) as clock:
    ids = {}
    for kind, at in [
      ("clock_in", _ny(19, 9)),
      ("break_start", _ny(19, 10)),
      ("break_end", _ny(19, 10, 17, 30)),
      ("lunch_start", _ny(19, 12)),
      ("lunch_end", _ny(19, 12, 30)),
      ("break_start", _ny(19, 15)),
      ("break_end", _ny(19, 15, 15, 59)),
      ("clock_in", _ny(20, 9)),
      ("clock_out", _ny(20, 9, 30)),
    ]:
      ids[kind, at] = uuid4()
      clock.execute(
        "INSERT INTO punches (id, employee_id, kind, at, instance_id) VALUES (%s, %s, %s, %s, 'main-warehouse')",
        (ids[kind, at], ann, kind, at),
      )
  with psycopg.connect(fresh_db, autocommit=True) as c:
    c.execute(
      "INSERT INTO punches (id, employee_id, kind, at, instance_id, automatic) VALUES (%s, %s, 'clock_out', %s, 'backend', true)",
      (uuid4(), ann, _ny(19, 17)),
    )
    c.execute(
      "INSERT INTO punch_corrections (punch_id, action, new_at) VALUES (%s, 'amend', %s)",
      (ids["clock_out", _ny(20, 9, 30)], _ny(20, 10)),
    )
    rows = c.execute(
      "SELECT day, week_of, clock, clock_in, clock_out, automatic_clock_out, corrected, shift_length, hours, breaks,"
      " unpaid_breaks, lunches, break_count, lunch_count, breaks_over_limit, break_overage, break_times, lunch_times"
      " FROM shift_summary"
    ).fetchall()
  monday, tuesday = date(2026, 10, 19), date(2026, 10, 20)
  assert rows == [
    (
      tuesday,
      monday,
      "main-warehouse",
      _ny(20, 9),
      _ny(20, 10),
      False,
      True,
      "1:00",
      "1:00",
      "0:00",
      "0:00",
      "0:00",
      0,
      0,
      0,
      "0:00",
      "",
      "",
    ),
    (
      *(monday, monday, "main-warehouse", _ny(19, 9), _ny(19, 17), True, False),
      *("8:00", "6:56", "0:30", "0:03", "0:30", 2, 1, 1, "0:02", "10:00-10:17 (+2), 15:00-15:15", "12:00-12:30"),
    ),
  ]


def test_the_break_limit_caps_each_break_in_every_hours_view(fresh_db: str):
  # Monday: breaks of 20 and 12 minutes against the daily 30. Per limit, each view's paid and unpaid break time, and
  # shift_summary's over-limit columns, which follow the limit in whole minutes and flag nothing at 0.
  with psycopg.connect(fresh_db, autocommit=True) as c:
    c.execute("INSERT INTO backend.environment (tz) VALUES ('America/New_York')")
    ann = _add_employee(c)
    for kind, at in [
      ("clock_in", _ny(19, 9)),
      ("break_start", _ny(19, 10)),
      ("break_end", _ny(19, 10, 20)),
      ("break_start", _ny(19, 13)),
      ("break_end", _ny(19, 13, 12)),
      ("clock_out", _ny(19, 17)),
    ]:
      _add_punch_at(c, ann, kind, at)
    assert c.execute("SELECT break_limit_minutes FROM config.settings").fetchone() == (15,)
    got = {}
    for limit in (15, 10, 0):
      c.execute("UPDATE config.settings SET break_limit_minutes = %s", (limit,))
      got[limit] = [
        *(
          c.execute(sql.SQL("SELECT breaks, unpaid_breaks FROM {}").format(sql.Identifier(view))).fetchone()
          for view in ("weekly_hours", "daily_hours")
        ),
        c.execute("SELECT breaks, unpaid_breaks, breaks_over_limit, break_overage, break_times FROM shift_summary").fetchone(),
      ]
  assert got == {
    15: [("0:27", "0:05"), ("0:27", "0:05"), ("0:27", "0:05", 1, "0:05", "10:00-10:20 (+5), 13:00-13:12")],
    10: [("0:20", "0:12"), ("0:20", "0:12"), ("0:20", "0:12", 2, "0:12", "10:00-10:20 (+10), 13:00-13:12 (+2)")],
    0: [("0:30", "0:02"), ("0:30", "0:02"), ("0:30", "0:02", 0, "0:00", "10:00-10:20, 13:00-13:12")],
  }


def test_new_employee_names_are_capitalized_unless_typed_mixed(fresh_db: str):
  typed_and_stored = [
    ("don", "Don"),
    ("SMITH", "Smith"),
    ("o'brien", "O'Brien"),
    ("SMITH-JONES", "Smith-Jones"),
    ("deNiro", "Deniro"),  # a lowercase first letter
    ("josé", "José"),
    ("DeNiro", "DeNiro"),
    ("McBee", "McBee"),
    ("Ann", "Ann"),
  ]
  with psycopg.connect(fresh_db, autocommit=True) as c:
    stored = [
      c.execute(
        "INSERT INTO employees (first_name, middle_name, last_name, pin_username, pin_hash)"
        " VALUES (%s, %s, %s, %s, crypt('1234', gen_salt('bf'))) RETURNING first_name, middle_name, last_name",
        (typed, typed, typed, str(n)),
      ).fetchone()
      for n, (typed, _) in enumerate(typed_and_stored)
    ]
    no_middle = c.execute(
      "INSERT INTO employees (first_name, last_name, pin_username, pin_hash) VALUES ('ann', 'lee', '99', crypt('1234', gen_salt('bf')))"
      " RETURNING id, middle_name"
    ).fetchone()
    assert no_middle is not None
    # Inserts only: the owner's edit is kept as typed.
    edited = c.execute("UPDATE employees SET first_name = 'deNiro' WHERE id = %s RETURNING first_name", (no_middle[0],)).fetchone()
  assert stored == [(s, s, s) for _, s in typed_and_stored]
  assert no_middle[1] is None
  assert edited == ("deNiro",)


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
      c.execute("INSERT INTO employees (first_name, last_name, pin_username, pin_hash) VALUES ('Bob', 'Lee', '34', '1234')")


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
        c.execute("INSERT INTO employees (first_name, last_name, pin_username, pin_hash) VALUES ('Bob', 'Lee', '34', '1234')")
  finally:
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
      admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


def test_employee_ids_are_by_default(fresh_db: str):
  # A clock supplies the number it chose (§6.4); a hand-added row still takes the sequence.
  insert = "INSERT INTO employees (id, first_name, last_name, pin_username, pin_hash) VALUES (%s, 'Ann', 'Lee', %s, crypt('1', gen_salt('bf')))"
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
  insert = sql.SQL(
    "INSERT INTO employees (first_name, last_name, pin_username, pin_hash, {}) VALUES ('Ann', 'Lee', '12', crypt('1', gen_salt('bf')), %s)"
  )
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
    "INSERT INTO employees (id, first_name, middle_name, last_name, pin_username, pin_hash, role, group_id, phone, email)"
    " VALUES (%s, 'Ann', 'B', 'Lee', %s, crypt('1', gen_salt('bf')), %s, 1, '+15551234567', 'ann@example.com')"
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


def test_a_temporary_pg_roles_doesnt_let_a_clock_add_an_admin(fresh_db: str, clock_login: str):
  # A session's temporary tables come before pg_catalog unless the search_path lists pg_temp. Production revokes
  # TEMPORARY from the clocks; a fresh database still grants it to PUBLIC.
  with psycopg.connect(clock_login, autocommit=True) as c:
    c.execute("CREATE TEMPORARY TABLE pg_roles (rolname name, rolsuper boolean)")
    c.execute("INSERT INTO pg_temp.pg_roles VALUES (current_user, true)")
    with pytest.raises(errors.CheckViolation):
      c.execute(
        "INSERT INTO employees (id, first_name, last_name, pin_username, pin_hash, role, group_id)"
        " VALUES (10003, 'Ann', 'Lee', '10003', crypt('1', gen_salt('bf')), 'admin', 1)"
      )


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
  "statement",
  [
    "UPDATE config.settings SET paid_break_minutes = -1",
    "UPDATE config.settings SET weekly_hours_alert = 0",
    "UPDATE config.settings SET break_limit_minutes = -1",
  ],
)
def test_hours_settings_checks(fresh_db: str, statement: LiteralString):
  with psycopg.connect(fresh_db, autocommit=True) as c, pytest.raises(errors.CheckViolation):
    c.execute(statement)


def _add_print(c: psycopg.Connection, employee_id: int, finger: str = "left_index") -> str:
  fingerprint_id = str(uuid4())
  c.execute(
    "INSERT INTO fingerprints (id, employee_id, finger, template, registered_at, instance_id) VALUES (%s, %s, %s, 't', %s, '1')",
    (fingerprint_id, employee_id, finger, T),
  )
  return fingerprint_id


def test_finger_values_match_the_sql(fresh_db: str):
  with psycopg.connect(fresh_db) as c:
    assert c.execute("SELECT enum_range(NULL::finger)::text").fetchone() == ("{" + ",".join(Finger) + "}",)


def test_fingerprints_need_a_finger(fresh_db: str):
  with psycopg.connect(fresh_db, autocommit=True) as c:
    employee_id = _add_employee(c)
    with pytest.raises(errors.NotNullViolation):
      c.execute(
        "INSERT INTO fingerprints (id, employee_id, template, registered_at, instance_id) VALUES (%s, %s, 't', %s, '1')",
        (uuid4(), employee_id, T),
      )


def test_one_print_per_finger(fresh_db: str):
  with psycopg.connect(fresh_db, autocommit=True) as c:
    ann, bob = _add_employee(c), _add_employee(c, "Bob", "34")
    _add_print(c, ann)
    with pytest.raises(errors.UniqueViolation):
      _add_print(c, ann)
    _add_print(c, ann, "right_index")
    _add_print(c, bob)
    assert c.execute("SELECT count(*) FROM fingerprints").fetchone() == (3,)


def test_clock_login_can_delete_prints(fresh_db: str, clock_login: str):
  with psycopg.connect(fresh_db, autocommit=True) as admin:
    employee_id = _add_employee(admin)
  with psycopg.connect(clock_login, autocommit=True) as c:
    fingerprint_id = _add_print(c, employee_id)
    c.execute("DELETE FROM fingerprints WHERE id = %s", (fingerprint_id,))
    assert c.execute("SELECT count(*) FROM fingerprints").fetchone() == (0,)


_CHANGE_INSERT: LiteralString = (
  "INSERT INTO fingerprint_changes (id, employee_id, finger, action, approved_by, pin_only, at, instance_id)"
  " VALUES (%s, %s, 'left_index', %s, %s, %s, %s, '1')"
)


@pytest.mark.parametrize(
  ("action", "approved", "pin_only", "ok"),
  [
    ("add", False, None, True),
    ("add", True, False, True),
    ("replace", True, True, True),
    ("remove", True, False, True),
    ("replace", False, None, False),
    ("remove", False, None, False),
    ("add", True, None, False),
    ("add", False, True, False),
  ],
)
def test_fingerprint_change_checks(fresh_db: str, action: str, *, approved: bool, pin_only: bool | None, ok: bool):
  with psycopg.connect(fresh_db, autocommit=True) as c:
    employee_id, admin_id = _add_employee(c), _add_employee(c, "Ada", "34")
    params = (uuid4(), employee_id, action, admin_id if approved else None, pin_only, T)
    if ok:
      c.execute(_CHANGE_INSERT, params)
    else:
      with pytest.raises(errors.CheckViolation):
        c.execute(_CHANGE_INSERT, params)


def test_clock_login_inserts_fingerprint_changes(fresh_db: str, clock_login: str):
  with psycopg.connect(fresh_db, autocommit=True) as admin:
    employee_id, admin_id = _add_employee(admin), _add_employee(admin, "Ada", "34")
  role = conninfo_to_dict(clock_login)["user"]
  change_id = uuid4()
  with psycopg.connect(clock_login, autocommit=True) as c:
    for _ in range(2):
      c.execute(_CHANGE_INSERT + " ON CONFLICT (id) DO NOTHING", (change_id, employee_id, "remove", admin_id, False, T))
    for statement in ("UPDATE fingerprint_changes SET pin_only = true", "DELETE FROM fingerprint_changes"):
      with pytest.raises(errors.InsufficientPrivilege):
        c.execute(statement)
  with psycopg.connect(fresh_db) as admin:
    assert admin.execute("SELECT count(*), min(db_login) FROM fingerprint_changes").fetchone() == (1, role)


def test_0002_refuses_existing_prints(admin_dsn: str):
  name = f"tcs_{uuid4().hex}"
  with psycopg.connect(admin_dsn, autocommit=True) as admin:
    admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
  try:
    first, second = load_migrations()[:2]
    with psycopg.connect(make_conninfo(admin_dsn, dbname=name), autocommit=True) as c:
      c.execute(cast("LiteralString", first.sql))
      employee_id = _add_employee(c)
      c.execute(
        "INSERT INTO fingerprints (id, employee_id, template, registered_at, instance_id) VALUES (%s, %s, 't', %s, '1')",
        (uuid4(), employee_id, T),
      )
      with pytest.raises(errors.RaiseException, match="fingerprints"):
        c.execute(cast("LiteralString", second.sql))
  finally:
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
      admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


def test_every_table_loads_into_its_model(fresh_db: str, make_editor: Callable[..., str]):
  with psycopg.connect(fresh_db, autocommit=True) as c:
    c.execute("INSERT INTO groups (name) VALUES ('night')")
    employee_id, manager_id = _add_employee(c), _add_employee(c, "Bob", "34")
    punch_id = _add_punch(c, employee_id)
    row = c.execute("INSERT INTO edits (note) VALUES ('n') RETURNING id").fetchone()
    assert row is not None
    edit_id = row[0]
    c.execute("UPDATE punches SET edit_id = %s WHERE id = %s", (edit_id, punch_id))
    c.execute("UPDATE employees SET last_name = 'Park' WHERE id = %s", (employee_id,))
    make_editor(employee_id)
    c.execute(
      "INSERT INTO fingerprints (id, employee_id, finger, template, registered_at, instance_id) VALUES (%s, %s, 'left_index', %s, %s, '1')",
      (uuid4(), employee_id, b"tmpl", T),
    )
    c.execute(
      "INSERT INTO fingerprint_changes (id, employee_id, finger, action, approved_by, pin_only, at, instance_id)"
      " VALUES (%s, %s, 'left_index', 'replace', %s, true, %s, '1')",
      (uuid4(), employee_id, manager_id, T),
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
    c.execute("INSERT INTO backend.scheduled_runs (name, period, scheduled_for) VALUES ('payroll_report', '2026-11-02', %s)", (T,))
    tables: list[tuple[LiteralString, type[TimeclockModel]]] = [
      ("groups", Group),
      ("employees", Employee),
      ("fingerprints", Fingerprint),
      ("punches", Punch),
      ("punch_corrections", PunchCorrection),
      ("fingerprint_bypasses", FingerprintBypass),
      ("fingerprint_changes", FingerprintChange),
      ("rules", RuleRow),
      ("config.business_hours", BusinessHours),
      ("config.settings", Settings),
      ("backend.auto_clock_out_runs", AutoClockOutRun),
      ("edits", Edit),
      ("employee_changes", EmployeeChange),
      ("backend.editors", Editor),
      ("backend.scheduled_runs", ScheduledRun),
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
    "fingerprint_changes": 1,
    "rules": 1,
    "config.business_hours": 1,
    "config.settings": 1,
    "backend.auto_clock_out_runs": 7,
    "edits": 1,
    "employee_changes": 1,
    "backend.editors": 1,
    "backend.scheduled_runs": 1,
  }
  assert loaded["config.business_hours"][0] == BusinessHours(id=1, weekday=7, opens=time(9), closes=time(23, 59, 59))
  assert loaded["rules"][0].model_dump()["params"] == {"n": 1}
  assert loaded["fingerprints"][0].model_dump()["template"].get_secret_value() == b"tmpl"
  assert loaded["punches"][0].model_dump()["edit_id"] == edit_id
  assert loaded["punch_corrections"][0].model_dump()["db_login"] == conninfo_to_dict(fresh_db)["user"]
  assert loaded["employee_changes"][0].model_dump()["field"] == "last_name"


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


def test_settings_default_the_scan_switches(fresh_db: str):
  with psycopg.connect(fresh_db, autocommit=True) as c:
    assert c.execute("SELECT save_scan_images, fingerprint_match_margin FROM config.settings").fetchone() == (True, 15)
    with pytest.raises(errors.CheckViolation):
      c.execute("UPDATE config.settings SET fingerprint_match_margin = -1")


def test_settings_default_the_registration_bars(fresh_db: str):
  with psycopg.connect(fresh_db, autocommit=True) as c:
    row = c.execute(
      "SELECT registration_min_detail_points, registration_max_detail_points, registration_centre_tolerance,"
      " registration_agreement_bar, registration_agreement_needed, registration_resemblance_bar, registration_disagreement_retries"
      " FROM config.settings"
    ).fetchone()
    assert row == (20, 120, 100, 41, 1, 40, 5)
    for update in (
      "UPDATE config.settings SET registration_min_detail_points = -1",
      "UPDATE config.settings SET registration_max_detail_points = 19",
      "UPDATE config.settings SET registration_centre_tolerance = -1",
      "UPDATE config.settings SET registration_agreement_bar = -1",
      "UPDATE config.settings SET registration_agreement_needed = -1",
      "UPDATE config.settings SET registration_agreement_needed = 5",
      "UPDATE config.settings SET registration_resemblance_bar = -1",
      "UPDATE config.settings SET registration_disagreement_retries = -1",
    ):
      with pytest.raises(errors.CheckViolation):
        c.execute(update)


_SCAN_COLUMNS = (
  "id, at, instance_id, result, best_fingerprint_id, best_employee_id, best_finger, best_score, other_fingerprint_id,"
  " other_employee_id, other_finger, other_score, scores, margin, outcome, punch_id, retry_message, probe, image,"
  " image_width, image_height"
)


def test_clock_login_inserts_scans_only(fresh_db: str, clock_login: str):
  with psycopg.connect(fresh_db, autocommit=True) as admin:
    employee_id = _add_employee(admin)
    punch_id = _add_punch(admin, employee_id)
    print_id = _add_print(admin, employee_id)
  role = conninfo_to_dict(clock_login)["user"]
  scan_id = uuid4()
  insert = (
    f"INSERT INTO fingerprint_scans ({_SCAN_COLUMNS}) VALUES (%s, %s, '1', 'matched', %s, %s, 'left_index', 90, %s, %s,"
    " 'right_index', 50, %s::jsonb, 15, 'punched', %s, 'msg', %s, %s, 160, 120)"
  )
  params = (scan_id, T, print_id, employee_id, print_id, employee_id, f'{{"{print_id}": 40}}', punch_id, b"probe", b"image")
  with psycopg.connect(clock_login, autocommit=True) as c:
    for _ in range(2):
      c.execute(insert + " ON CONFLICT (id) DO NOTHING", params)
    for statement in (
      "SELECT image FROM fingerprint_scans",
      "UPDATE fingerprint_scans SET margin = 1",
      "DELETE FROM fingerprint_scans",
    ):
      with pytest.raises(errors.InsufficientPrivilege):
        c.execute(cast("LiteralString", statement))
  with psycopg.connect(fresh_db) as admin:
    assert admin.execute("SELECT count(*), min(db_login), bool_and(received_at IS NOT NULL) FROM fingerprint_scans").fetchone() == (
      1,
      role,
      True,
    )


def test_scan_log_names_prints_and_outcome(fresh_db: str):
  now = datetime.now(UTC)  # not T: a scan over 28 days old loses its image at the next insert
  with psycopg.connect(fresh_db, autocommit=True) as c:
    employee_id, other_id = _add_employee(c), _add_employee(c, "Bob", "34")
    punch_id = _add_punch(c, employee_id)
    c.execute("UPDATE employees SET middle_name = 'Q' WHERE id = %s", (employee_id,))
    best_print, other_print = _add_print(c, employee_id), _add_print(c, other_id, "right_thumb")
    c.execute(
      "INSERT INTO fingerprint_scans (id, at, instance_id, result, best_fingerprint_id, best_employee_id, best_finger,"
      " best_score, other_fingerprint_id, other_employee_id, other_finger, other_score, margin, outcome, punch_id, image)"
      " VALUES (%s, %s, '1', 'matched', %s, %s, 'left_index', 90, %s, %s, 'right_thumb', 60, 15, 'punched', %s, %s)",
      (uuid4(), now, best_print, employee_id, other_print, other_id, punch_id, b"img"),
    )
    c.execute(
      "INSERT INTO fingerprint_scans (id, at, instance_id, result, margin, retry_message) VALUES (%s, %s, '1', 'retry', 15, 'move')",
      (uuid4(), now + timedelta(minutes=1)),
    )
    rows = c.cursor(row_factory=dict_row).execute("SELECT * FROM scan_log").fetchall()
  assert [r["result"] for r in rows] == ["retry", "matched"]
  retry, matched = rows
  assert retry["retry_message"] == "move"
  assert (retry["employee_name"], retry["other_name"], retry["finger"], retry["has_image"]) == (None, None, None, False)
  assert matched["employee_name"] == "Ann Q Lee"
  assert matched["other_name"] == "Bob Lee"
  assert (matched["finger"], matched["score"], matched["other_finger"], matched["other_score"]) == (
    "left_index",
    90,
    "right_thumb",
    60,
  )
  assert (matched["margin"], matched["outcome"], matched["punch_kind"], matched["has_image"]) == (15, "punched", "clock_in", True)
  assert "image" not in matched and "probe" not in matched


_INSERT_SCAN_WITH_DATA = (
  "INSERT INTO fingerprint_scans (id, at, instance_id, result, margin, probe, image, image_width, image_height)"
  " VALUES (%s, %s, '1', 'no_match', 15, 'probe', 'image', 160, 120)"
)
_CLEARED = (
  "SELECT probe IS NULL AND image IS NULL AND image_width IS NULL AND image_height IS NULL FROM fingerprint_scans WHERE id = %s"
)


def test_a_clock_insert_clears_fingerprint_data_older_than_28_days(fresh_db: str, clock_login: str):
  now = datetime.now(UTC)
  old, recent, late, new = uuid4(), uuid4(), uuid4(), uuid4()
  with psycopg.connect(fresh_db, autocommit=True) as admin:
    for scan_id in (old, recent):
      admin.execute(_INSERT_SCAN_WITH_DATA, (scan_id, now - timedelta(days=27)))
    # Aged by UPDATE: an insert this old would be cleared by its own statement.
    admin.execute("UPDATE fingerprint_scans SET at = %s WHERE id = %s", (now - timedelta(days=29), old))
  with psycopg.connect(clock_login, autocommit=True) as c:
    c.execute(_INSERT_SCAN_WITH_DATA, (new, now))
    c.execute(_INSERT_SCAN_WITH_DATA, (late, now - timedelta(days=30)))  # a clock that was offline a month
  with psycopg.connect(fresh_db) as admin:
    cleared = {s: admin.execute(_CLEARED, (s,)).fetchone() for s in (old, recent, late, new)}
    kept = admin.execute("SELECT result, margin FROM fingerprint_scans WHERE id = %s", (old,)).fetchone()
  assert cleared == {old: (True,), recent: (False,), late: (True,), new: (False,)}
  assert kept == ("no_match", 15)


def test_a_temporary_fingerprint_scans_doesnt_take_a_clocks_prune(fresh_db: str, clock_login: str):
  # The prune runs as its owner but in the clock's session, where a temporary table of the same name would come first.
  now, old = datetime.now(UTC), uuid4()
  with psycopg.connect(fresh_db, autocommit=True) as admin:
    admin.execute(_INSERT_SCAN_WITH_DATA, (old, now))
    admin.execute("UPDATE fingerprint_scans SET at = %s WHERE id = %s", (now - timedelta(days=29), old))
  with psycopg.connect(clock_login, autocommit=True) as c:
    c.execute("CREATE TEMPORARY TABLE fingerprint_scans (at timestamptz, probe bytea, image bytea, image_width int, image_height int)")
    c.execute(_INSERT_SCAN_WITH_DATA.replace("INTO fingerprint_scans", "INTO public.fingerprint_scans"), (uuid4(), now))
  with psycopg.connect(fresh_db) as admin:
    assert admin.execute(_CLEARED, (old,)).fetchone() == (True,)


def test_an_insert_skips_the_prune_while_another_runs_it(fresh_db: str, clock_login: str):
  now, old = datetime.now(UTC), uuid4()
  with psycopg.connect(fresh_db, autocommit=True) as admin:
    admin.execute(_INSERT_SCAN_WITH_DATA, (old, now))
    admin.execute("UPDATE fingerprint_scans SET at = %s WHERE id = %s", (now - timedelta(days=29), old))
  with psycopg.connect(fresh_db) as pruning, psycopg.connect(clock_login, autocommit=True) as c:
    pruning.execute("SELECT pg_advisory_xact_lock(7461726504)")  # held until rollback, like a prune under way
    c.execute(_INSERT_SCAN_WITH_DATA, (uuid4(), now))  # doesn't wait for the lock
    assert pruning.execute(_CLEARED, (old,)).fetchone() == (False,)
    pruning.rollback()
    c.execute(_INSERT_SCAN_WITH_DATA, (uuid4(), now))
    assert pruning.execute(_CLEARED, (old,)).fetchone() == (True,)


# The shift corrections page (spec 2026-11-05): the editors allowlist and each account's database role, its "badge".


def test_editor_row_creates_a_badge_the_page_can_become(fresh_db: str, web_login: str, make_editor: Callable[..., str]):
  email = make_editor()
  with psycopg.connect(fresh_db) as admin:
    assert admin.execute(
      "SELECT rolcanlogin, pg_has_role(%s, 'timeclock_editor_access', 'USAGE') FROM pg_roles WHERE rolname = %s", (email, email)
    ).fetchone() == (False, True)
  with psycopg.connect(web_login, autocommit=True) as c:
    c.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(email)))
    assert c.execute("SELECT current_user::text, session_user::text").fetchone() == (email, "timeclock_web")


def test_page_login_has_no_privileges_of_its_own(web_login: str, make_editor: Callable[..., str]):
  make_editor()
  with psycopg.connect(web_login, autocommit=True) as c:
    statements: list[LiteralString] = [
      "SELECT * FROM punches",
      "SELECT * FROM employees",
      "SELECT * FROM backend.editors",
      "SET ROLE timeclock_editor_access",
    ]
    for statement in statements:
      with pytest.raises(errors.InsufficientPrivilege):
        c.execute(statement)


def test_badge_cannot_become_the_group_role(web_login: str, make_editor: Callable[..., str]):
  email = make_editor()
  with psycopg.connect(web_login, autocommit=True) as c:
    c.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(email)))
    with pytest.raises(errors.InsufficientPrivilege):
      c.execute("SET ROLE timeclock_editor_access")


def test_deleting_the_row_drops_the_badge_and_readding_restores_it(fresh_db: str, make_editor: Callable[..., str]):
  email = make_editor()
  with psycopg.connect(fresh_db, autocommit=True) as c:
    c.execute("DELETE FROM backend.editors WHERE email = %s", (email,))
    assert not _role_exists(c, email)
    c.execute("INSERT INTO backend.editors (email) VALUES (%s)", (email,))
    assert _role_exists(c, email)


def test_changing_the_email_moves_the_badge(fresh_db: str, admin_dsn: str, make_editor: Callable[..., str]):
  email, moved = make_editor(), _new_email()
  try:
    with psycopg.connect(fresh_db, autocommit=True) as c:
      c.execute("UPDATE backend.editors SET email = %s WHERE email = %s", (moved, email))
      assert not _role_exists(c, email)
      assert _role_exists(c, moved)
      assert c.execute("SELECT pg_has_role('timeclock_web', %s, 'SET')", (moved,)).fetchone() == (True,)
  finally:
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
      admin.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(moved)))


def test_test_database_allowlist_touches_no_roles(fresh_db: str, make_editor: Callable[..., str]):
  email = make_editor()  # made while the database counts as production, so the badge exists
  with psycopg.connect(fresh_db, autocommit=True) as c:
    c.execute("INSERT INTO backend.deployment (production) VALUES (false)")
    c.execute("DELETE FROM backend.editors WHERE email = %s", (email,))
    assert _role_exists(c, email)
    other = _new_email()
    c.execute("INSERT INTO backend.editors (email) VALUES (%s)", (other,))
    assert not _role_exists(c, other)


@pytest.mark.parametrize("email", ["Pat@example.com", "not-an-email", "a" * 52 + "@example.com"])  # the last is 64 bytes
def test_editor_email_checks(fresh_db: str, email: str):
  with psycopg.connect(fresh_db) as c, pytest.raises(errors.CheckViolation):
    c.execute("INSERT INTO backend.editors (email) VALUES (%s)", (email,))


def test_editor_account_looks_up_any_case(fresh_db: str, web_login: str, make_editor: Callable[..., str]):
  with psycopg.connect(fresh_db, autocommit=True) as admin:
    employee_id = _add_employee(admin)
  email = make_editor(employee_id, developer=True)
  with psycopg.connect(web_login) as c:
    assert c.execute("SELECT * FROM backend.editor_account(%s)", (email.upper(),)).fetchall() == [(employee_id, True)]
    assert c.execute("SELECT * FROM backend.editor_account('nobody@example.com')").fetchall() == []


def test_only_the_page_login_may_look_up_editors(fresh_db: str):
  with psycopg.connect(fresh_db) as c:
    may = "SELECT has_function_privilege(%s, 'backend.editor_account(text)', 'EXECUTE')"
    assert c.execute(may, ("timeclock_web",)).fetchone() == (True,)
    assert c.execute(may, ("timeclock_editor_access",)).fetchone() == (False,)
    assert c.execute(may, ("timeclock_clock_access",)).fetchone() == (False,)


@contextmanager
def _as_badge(web_login: str, email: str) -> Generator[psycopg.Connection]:
  """The page's connection: timeclock_web acting as the account's badge. Not autocommit; write in `c.transaction()`."""
  with psycopg.connect(web_login) as c:
    c.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(email)))
    c.commit()
    yield c


# A punch from the page, in a save of its own.
_PAGE_PUNCH: LiteralString = (
  "WITH e AS (INSERT INTO edits (note) VALUES ('') RETURNING id)"
  " INSERT INTO punches (employee_id, kind, at, instance_id, edit_id) SELECT %s, 'clock_in', %s, 'web', e.id FROM e"
)
_OWN = "you can't change your own punches"


def test_badge_adds_a_save_a_punch_and_a_correction(fresh_db: str, web_login: str, make_editor: Callable[..., str]):
  with psycopg.connect(fresh_db, autocommit=True) as admin:
    editor_id = _add_employee(admin)
    other_id = _add_employee(admin, "Bob", "34")
    punch_id = _add_punch(admin, other_id)
  email = make_editor(editor_id)
  with _as_badge(web_login, email) as c, c.transaction():
    row = c.execute("INSERT INTO edits (note) VALUES ('left at 5') RETURNING id, db_login").fetchone()
    assert row is not None
    edit_id, login = row
    assert login == email
    c.execute(
      "INSERT INTO punches (employee_id, kind, at, instance_id, edit_id) VALUES (%s, 'clock_out', %s, 'web', %s)",
      (other_id, T + timedelta(hours=8), edit_id),
    )
    c.execute(
      "INSERT INTO punch_corrections (punch_id, action, new_at, note, edit_id) VALUES (%s, 'amend', %s, 'came in early', %s)",
      (punch_id, T - timedelta(minutes=5), edit_id),
    )
  with psycopg.connect(fresh_db) as admin:
    assert admin.execute("SELECT db_login, edit_id FROM punches WHERE edit_id IS NOT NULL").fetchall() == [(email, edit_id)]
    assert admin.execute("SELECT db_login, edit_id FROM punch_corrections").fetchall() == [(email, edit_id)]


def test_badge_cannot_touch_its_own_punches(fresh_db: str, web_login: str, make_editor: Callable[..., str]):
  with psycopg.connect(fresh_db, autocommit=True) as admin:
    own_id = _add_employee(admin)
    punch_id = _add_punch(admin, own_id)
  with _as_badge(web_login, make_editor(own_id)) as c:
    with pytest.raises(errors.InsufficientPrivilege, match=_OWN), c.transaction():
      c.execute(_PAGE_PUNCH, (own_id, T))
    with pytest.raises(errors.InsufficientPrivilege, match=_OWN), c.transaction():
      c.execute("INSERT INTO punch_corrections (punch_id, action) VALUES (%s, 'void')", (punch_id,))


def test_editor_without_an_employee_number_has_no_block(fresh_db: str, web_login: str, make_editor: Callable[..., str]):
  with psycopg.connect(fresh_db, autocommit=True) as admin:
    employee_id = _add_employee(admin)
    punch_id = _add_punch(admin, employee_id)
  with _as_badge(web_login, make_editor(None)) as c, c.transaction():
    c.execute(_PAGE_PUNCH, (employee_id, T))
    c.execute("INSERT INTO punch_corrections (punch_id, action) VALUES (%s, 'void')", (punch_id,))


def test_removed_editor_is_refused_at_once(fresh_db: str, web_login: str, make_editor: Callable[..., str]):
  email = make_editor(None)
  with psycopg.connect(fresh_db, autocommit=True) as admin:
    employee_id = _add_employee(admin)
    # Keeps the badge, as a session that SET ROLE before the row went would still hold it.
    admin.execute("ALTER TABLE backend.editors DISABLE TRIGGER editors_badge")
    admin.execute("DELETE FROM backend.editors WHERE email = %s", (email,))
  with _as_badge(web_login, email) as c, pytest.raises(errors.InsufficientPrivilege, match="not on the allowlist"), c.transaction():
    c.execute(_PAGE_PUNCH, (employee_id, T))


def test_test_database_blocks_by_the_page_supplied_number(fresh_db: str, web_login: str, make_editor: Callable[..., str]):
  email = make_editor(None)  # made while the database counts as production, so the badge exists
  with psycopg.connect(fresh_db, autocommit=True) as admin:
    employee_id = _add_employee(admin)
    admin.execute("INSERT INTO backend.deployment (production) VALUES (false)")
    admin.execute("DELETE FROM backend.editors WHERE email = %s", (email,))  # the test database's list is never read
  with _as_badge(web_login, email) as c:
    with pytest.raises(errors.InsufficientPrivilege, match=_OWN), c.transaction():
      c.execute("SELECT set_config('timeclock.editor_employee_id', %s, true)", (str(employee_id),))
      c.execute(_PAGE_PUNCH, (employee_id, T))
    with c.transaction():
      c.execute(_PAGE_PUNCH, (employee_id, T))


def test_production_ignores_a_page_supplied_number(fresh_db: str, web_login: str, make_editor: Callable[..., str]):
  with psycopg.connect(fresh_db, autocommit=True) as admin:
    employee_id = _add_employee(admin)
  with _as_badge(web_login, make_editor(None)) as c, c.transaction():
    c.execute("SELECT set_config('timeclock.editor_employee_id', %s, true)", (str(employee_id),))
    c.execute(_PAGE_PUNCH, (employee_id, T))


def test_badge_cannot_rewrite_history(fresh_db: str, web_login: str, make_editor: Callable[..., str]):
  with psycopg.connect(fresh_db, autocommit=True) as admin:
    employee_id = _add_employee(admin)
    punch_id = _add_punch(admin, employee_id)
    admin.execute("INSERT INTO punch_corrections (punch_id, action) VALUES (%s, 'void')", (punch_id,))
    admin.execute("INSERT INTO edits (note) VALUES ('x')")
    _add_print(admin, employee_id)
  forbidden: list[tuple[LiteralString, tuple[object, ...] | None]] = [
    ("UPDATE punches SET at = now()", None),
    ("DELETE FROM punches", None),
    ("UPDATE punch_corrections SET note = 'x'", None),
    ("DELETE FROM punch_corrections", None),
    ("INSERT INTO punches (employee_id, kind, at, instance_id, db_login) VALUES (%s, 'clock_in', %s, 'web', 'x')", (employee_id, T)),
    (
      "INSERT INTO punches (employee_id, kind, at, instance_id, automatic) VALUES (%s, 'clock_in', %s, 'web', true)",
      (employee_id, T),
    ),
    ("INSERT INTO punch_corrections (punch_id, action, db_login) VALUES (%s, 'void', 'x')", (punch_id,)),
    ("INSERT INTO edits (note, db_login) VALUES ('x', 'x')", None),
    ("UPDATE edits SET note = 'x'", None),
    ("DELETE FROM edits", None),
    ("SELECT template FROM fingerprints", None),
    ("SELECT * FROM backend.editors", None),
    ("SELECT * FROM backend.auto_clock_out_runs", None),
  ]
  with _as_badge(web_login, make_editor(None)) as c:
    for statement, params in forbidden:
      with pytest.raises(errors.InsufficientPrivilege), c.transaction():
        c.execute(statement, params)
    assert len(c.execute("SELECT id, finger FROM fingerprints").fetchall()) == 1


def test_clocks_and_owner_still_stamp_their_own_logins(fresh_db: str, clock_login: str):
  with psycopg.connect(fresh_db, autocommit=True) as admin:
    employee_id = _add_employee(admin)
  with psycopg.connect(clock_login, autocommit=True) as c:
    punch_id = _add_punch(c, employee_id)
  with psycopg.connect(fresh_db, autocommit=True) as admin:
    admin.execute("INSERT INTO punch_corrections (punch_id, action) VALUES (%s, 'void')", (punch_id,))
    assert admin.execute("SELECT p.db_login, c.db_login FROM punches p JOIN punch_corrections c ON c.punch_id = p.id").fetchone() == (
      conninfo_to_dict(clock_login)["user"],
      conninfo_to_dict(fresh_db)["user"],
    )


_OWN_SETTINGS = "you can't change your own role, group, fingerprint exemption or active status"


def test_employee_update_logs_each_changed_field(fresh_db: str, web_login: str, make_editor: Callable[..., str]):
  with psycopg.connect(fresh_db, autocommit=True) as admin:
    employee_id = _add_employee(admin)
    admin.execute("INSERT INTO groups (name) VALUES ('night')")  # id 2
  email = make_editor(None)
  with _as_badge(web_login, email) as c, c.transaction():
    row = c.execute("INSERT INTO edits (note) VALUES ('moved') RETURNING id").fetchone()
    assert row is not None
    (edit_id,) = row
    c.execute("SELECT set_config('timeclock.edit_id', %s, true)", (str(edit_id),))
    c.execute("UPDATE employees SET group_id = 2, email = 'a@example.com' WHERE id = %s", (employee_id,))
  with psycopg.connect(fresh_db) as admin:
    assert admin.execute("SELECT field, old_value, new_value, edit_id, db_login FROM employee_changes ORDER BY field").fetchall() == [
      ("email", None, "a@example.com", edit_id, email),
      ("group_id", "1", "2", edit_id, email),
    ]


def test_pin_change_is_logged_without_the_hash(fresh_db: str, web_login: str, make_editor: Callable[..., str]):
  with psycopg.connect(fresh_db, autocommit=True) as admin:
    employee_id = _add_employee(admin)
    row = admin.execute("SELECT crypt('5678', gen_salt('bf'))").fetchone()
    assert row is not None
    (pin_hash,) = row
  with _as_badge(web_login, make_editor(None)) as c, c.transaction():
    c.execute("UPDATE employees SET pin_hash = %s WHERE id = %s", (pin_hash, employee_id))
  with psycopg.connect(fresh_db) as admin:
    assert admin.execute("SELECT field, old_value, new_value FROM employee_changes").fetchall() == [("pin", None, None)]


def test_owner_edits_are_logged_too(fresh_db: str):
  with psycopg.connect(fresh_db, autocommit=True) as admin:
    employee_id = _add_employee(admin)
    admin.execute("UPDATE employees SET last_name = 'Park' WHERE id = %s", (employee_id,))
    assert admin.execute("SELECT field, old_value, new_value, edit_id, db_login FROM employee_changes").fetchall() == [
      ("last_name", "Lee", "Park", None, conninfo_to_dict(fresh_db)["user"])
    ]


def test_renumbering_an_employee_carries_their_log(fresh_db: str):
  with psycopg.connect(fresh_db, autocommit=True) as admin:
    employee_id = _add_employee(admin)
    admin.execute("UPDATE employees SET last_name = 'Park' WHERE id = %s", (employee_id,))
    admin.execute("UPDATE employees SET id = 9001, first_name = 'Annie' WHERE id = %s", (employee_id,))
    assert admin.execute("SELECT employee_id, field FROM employee_changes ORDER BY field").fetchall() == [
      (9001, "first_name"),
      (9001, "last_name"),
    ]


def test_editor_changes_own_name_contact_and_pin_but_not_the_rest(fresh_db: str, web_login: str, make_editor: Callable[..., str]):
  with psycopg.connect(fresh_db, autocommit=True) as admin:
    own_id = _add_employee(admin)
    other_id = _add_employee(admin, "Bob", "34")
    admin.execute("INSERT INTO groups (name) VALUES ('night')")  # id 2
    row = admin.execute("SELECT crypt('5678', gen_salt('bf'))").fetchone()
    assert row is not None
    (pin_hash,) = row
  settings: list[LiteralString] = [
    "UPDATE employees SET role = 'admin' WHERE id = %s",
    "UPDATE employees SET group_id = 2 WHERE id = %s",
    "UPDATE employees SET fingerprint_exempt = true WHERE id = %s",
    "UPDATE employees SET active = false WHERE id = %s",
  ]
  with _as_badge(web_login, make_editor(own_id)) as c:
    with c.transaction():
      c.execute("UPDATE employees SET first_name = 'Annie', middle_name = 'May', last_name = 'Park' WHERE id = %s", (own_id,))
      c.execute("UPDATE employees SET phone = '+15555550123', email = 'ann@example.com' WHERE id = %s", (own_id,))
      c.execute("UPDATE employees SET pin_hash = %s WHERE id = %s", (pin_hash, own_id))
    for statement in settings:
      with pytest.raises(errors.InsufficientPrivilege, match=_OWN_SETTINGS), c.transaction():
        c.execute(statement, (own_id,))
      with c.transaction():
        c.execute(statement, (other_id,))


def test_removed_editor_cannot_edit_employees(fresh_db: str, web_login: str, make_editor: Callable[..., str]):
  email = make_editor(None)
  with psycopg.connect(fresh_db, autocommit=True) as admin:
    employee_id = _add_employee(admin)
    admin.execute("ALTER TABLE backend.editors DISABLE TRIGGER editors_badge")  # keeps the badge, as in the punch test
    admin.execute("DELETE FROM backend.editors WHERE email = %s", (email,))
  with _as_badge(web_login, email) as c, pytest.raises(errors.InsufficientPrivilege, match="not on the allowlist"), c.transaction():
    c.execute("UPDATE employees SET first_name = 'Annie' WHERE id = %s", (employee_id,))


def test_badge_can_make_an_admin_and_cannot_touch_other_columns(fresh_db: str, web_login: str, make_editor: Callable[..., str]):
  with psycopg.connect(fresh_db, autocommit=True) as admin:
    employee_id = _add_employee(admin)
  forbidden: list[tuple[LiteralString, tuple[object, ...] | None]] = [
    ("UPDATE employees SET pin_username = '9' WHERE id = %s", (employee_id,)),
    ("UPDATE employees SET id = 99 WHERE id = %s", (employee_id,)),
    ("DELETE FROM employees WHERE id = %s", (employee_id,)),
    ("INSERT INTO employee_changes (employee_id, field, db_login) VALUES (%s, 'role', 'x')", (employee_id,)),
    ("UPDATE employee_changes SET field = 'x'", None),
    ("DELETE FROM employee_changes", None),
  ]
  with _as_badge(web_login, make_editor(None)) as c:
    with c.transaction():
      c.execute("UPDATE employees SET role = 'admin' WHERE id = %s", (employee_id,))
    for statement, params in forbidden:
      with pytest.raises(errors.InsufficientPrivilege), c.transaction():
        c.execute(statement, params)


def test_page_added_punch_shows_its_note_and_counts_as_corrected(fresh_db: str, web_login: str, make_editor: Callable[..., str]):
  with psycopg.connect(fresh_db, autocommit=True) as admin:
    admin.execute("INSERT INTO backend.environment (tz) VALUES ('America/New_York')")
    employee_id = _add_employee(admin)
  with _as_badge(web_login, make_editor(None)) as c, c.transaction():
    row = c.execute("INSERT INTO edits (note) VALUES ('forgot to clock in') RETURNING id, recorded_at").fetchone()
    assert row is not None
    edit_id, saved_at = row
    for kind, at in [("clock_in", T - timedelta(hours=8)), ("clock_out", T)]:
      c.execute(
        "INSERT INTO punches (employee_id, kind, at, instance_id, edit_id) VALUES (%s, %s, %s, 'web', %s)",
        (employee_id, kind, at, edit_id),
      )
  with psycopg.connect(fresh_db) as admin:
    assert admin.execute("SELECT kind::text, correction_note, corrected_at FROM punch_log").fetchall() == [
      ("clock_in", "forgot to clock in", saved_at),
      ("clock_out", "forgot to clock in", saved_at),
    ]
    assert admin.execute("SELECT corrected FROM shift_summary").fetchall() == [(True,)]


def test_correction_note_still_wins_over_the_save(fresh_db: str, web_login: str, make_editor: Callable[..., str]):
  with psycopg.connect(fresh_db, autocommit=True) as admin:
    employee_id = _add_employee(admin)
  with _as_badge(web_login, make_editor(None)) as c, c.transaction():
    row = c.execute("INSERT INTO edits (note) VALUES ('forgot to clock in') RETURNING id").fetchone()
    assert row is not None
    row = c.execute(
      "INSERT INTO punches (employee_id, kind, at, instance_id, edit_id) VALUES (%s, 'clock_in', %s, 'web', %s) RETURNING id",
      (employee_id, T, row[0]),
    ).fetchone()
    assert row is not None
    punch_id = row[0]
  with psycopg.connect(fresh_db, autocommit=True) as admin:
    row = admin.execute(
      "INSERT INTO punch_corrections (punch_id, action, new_at, note) VALUES (%s, 'amend', %s, 'typo') RETURNING recorded_at",
      (punch_id, T - timedelta(hours=1)),
    ).fetchone()
    assert row is not None
    assert admin.execute("SELECT correction_note, corrected_at FROM punch_log").fetchall() == [("typo", row[0])]


# Final review fixes: the badge function can't be borrowed, temporary tables can't stand in for the real ones, page
# punches name their save, and badges never read PIN hashes.


def test_nobody_but_the_owner_may_run_the_badge_function(fresh_db: str, web_login: str, make_editor: Callable[..., str]):
  # It runs as its owner: on a table of the caller's own it would grant or drop any role.
  email = make_editor(None)
  with psycopg.connect(fresh_db) as c:
    may = "SELECT has_function_privilege(%s, 'backend.sync_editor_badge()', 'EXECUTE')"
    assert c.execute(may, ("timeclock_web",)).fetchone() == (False,)
    assert c.execute(may, (email,)).fetchone() == (False,)
  with _as_badge(web_login, email) as c, pytest.raises(errors.InsufficientPrivilege), c.transaction():
    c.execute("CREATE TEMP TABLE bait (email text)")
    c.execute("CREATE TRIGGER bait AFTER INSERT ON bait FOR EACH ROW EXECUTE FUNCTION backend.sync_editor_badge()")


def test_badge_function_serves_only_the_allowlist(fresh_db: str, admin_dsn: str):
  email = _new_email()
  try:
    with psycopg.connect(fresh_db, autocommit=True) as c:
      c.execute("CREATE TABLE bait (email text)")
      c.execute("CREATE TRIGGER bait AFTER INSERT ON bait FOR EACH ROW EXECUTE FUNCTION backend.sync_editor_badge()")
      with pytest.raises(errors.RaiseException, match=r"only backend\.editors"):
        c.execute("INSERT INTO bait VALUES (%s)", (email,))
  finally:
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
      admin.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(email)))


def test_temporary_tables_cannot_stand_in_for_the_real_ones(fresh_db: str, web_login: str, make_editor: Callable[..., str]):
  with psycopg.connect(fresh_db, autocommit=True) as admin:
    own_id = _add_employee(admin)
    other_id = _add_employee(admin, "Bob", "34")
    punch_id = _add_punch(admin, own_id)
  with _as_badge(web_login, make_editor(own_id)) as c:
    # A temporary punches table claiming the editor's punch is someone else's.
    with pytest.raises(errors.InsufficientPrivilege, match=_OWN), c.transaction():
      c.execute(
        sql.SQL("CREATE TEMP TABLE punches AS SELECT id, {}::bigint AS employee_id FROM public.punches").format(sql.Literal(other_id))
      )
      c.execute("INSERT INTO punch_corrections (punch_id, action) VALUES (%s, 'void')", (punch_id,))
    # A temporary change log catching the entry.
    with c.transaction():
      c.execute("CREATE TEMP TABLE employee_changes (LIKE public.employee_changes INCLUDING DEFAULTS)")
      c.execute("UPDATE employees SET role = 'admin' WHERE id = %s", (other_id,))
  with psycopg.connect(fresh_db) as admin:
    assert admin.execute("SELECT employee_id, field FROM employee_changes").fetchall() == [(other_id, "role")]


def test_badge_punches_must_name_their_save(fresh_db: str, web_login: str, make_editor: Callable[..., str]):
  # shift_summary counts a page punch as hand-made by its save; one without would pass for a clock's.
  with psycopg.connect(fresh_db, autocommit=True) as admin:
    employee_id = _add_employee(admin)
  with _as_badge(web_login, make_editor(None)) as c, pytest.raises(errors.CheckViolation, match="name its save"), c.transaction():
    c.execute("INSERT INTO punches (employee_id, kind, at, instance_id) VALUES (%s, 'clock_in', %s, 'web')", (employee_id, T))


def test_badge_cannot_read_pin_hashes(fresh_db: str, web_login: str, make_editor: Callable[..., str]):
  with psycopg.connect(fresh_db, autocommit=True) as admin:
    employee_id = _add_employee(admin)
  with _as_badge(web_login, make_editor(None)) as c:
    with pytest.raises(errors.InsufficientPrivilege), c.transaction():
      c.execute("SELECT pin_hash FROM employees")
    assert c.execute("SELECT id, first_name, pin_username FROM employees").fetchall() == [(employee_id, "Ann", "12")]


def test_new_employees_stamp_who_added_them(fresh_db: str, clock_login: str):
  with psycopg.connect(clock_login, autocommit=True) as c:
    by_clock = _add_employee(c)
  with psycopg.connect(fresh_db, autocommit=True) as admin:
    by_owner = _add_employee(admin, "Bob", "34")
    stamped = dict(admin.execute("SELECT id, db_login FROM employees").fetchall())
  assert stamped == {by_clock: conninfo_to_dict(clock_login)["user"], by_owner: conninfo_to_dict(fresh_db)["user"]}


def test_employees_from_before_0019_have_no_db_login(admin_dsn: str):
  # PostgreSQL evaluates a non-volatile ADD COLUMN default once and stamps it on every existing row: the migrating login.
  name = f"tcs_{uuid4().hex}"
  with psycopg.connect(admin_dsn, autocommit=True) as admin:
    admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
  try:
    with psycopg.connect(make_conninfo(admin_dsn, dbname=name), autocommit=True) as c:
      for m in load_migrations():
        if m.version == 19:  # noqa: PLR2004 - the migration under test
          before = _add_employee(c)
        with c.transaction():
          c.execute(cast("LiteralString", m.sql))  # package data, not user input
      after = _add_employee(c, "Bob", "34")
      stamped = dict(c.execute("SELECT id, db_login FROM employees").fetchall())
  finally:
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
      admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
  assert stamped == {before: None, after: conninfo_to_dict(admin_dsn)["user"]}


def test_badges_can_read_who_added_an_employee(fresh_db: str, web_login: str, make_editor: Callable[..., str]):
  with psycopg.connect(fresh_db, autocommit=True) as admin:
    employee_id = _add_employee(admin)
  with _as_badge(web_login, make_editor(None)) as c:
    assert c.execute("SELECT id, db_login FROM employees").fetchall() == [(employee_id, conninfo_to_dict(fresh_db)["user"])]
