-- timeclock-schema 0001: the tables, view and triggers of design §4, and the role `timeclock_clock_access` (created
-- only if absent, since roles are server-wide) with its grants. Plain SQL: runs unchanged in `timeclock` and
-- `timeclock_test` (R26.6). The backend's runner owns `schema_migrations`, the group roles and the `CONNECT` grants (§8.3).
-- Revised in place before any deployment (2026-10-27: employee roles, the lunch/break switches, the automatic clock-out's
-- run record), since no real database had applied it. Once one exists, every change is a new migration.

CREATE EXTENSION IF NOT EXISTS pgcrypto;  -- crypt()/gen_salt('bf') for PIN hashes (R23.1)

CREATE TYPE punch_kind AS ENUM ('clock_in', 'clock_out', 'break_start', 'break_end', 'lunch_start', 'lunch_end');
CREATE TYPE correction_action AS ENUM ('void', 'amend');
CREATE TYPE override_reason AS ENUM ('no_fingerprint', 'fingerprint_skipped');
CREATE TYPE rule_action AS ENUM ('refuse', 'warn', 'accept');
CREATE TYPE employee_role AS ENUM ('employee', 'manager', 'admin');  -- lowest to highest: an admin may do all a manager may
CREATE TYPE auto_clock_out_frequency AS ENUM ('daily');  -- later values: ALTER TYPE ... ADD VALUE

CREATE FUNCTION set_updated_at() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  NEW.updated_at := now();
  RETURN NEW;
END $$;

CREATE TABLE groups (
  id   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  name text NOT NULL UNIQUE
);
INSERT INTO groups (name) VALUES ('default');  -- id 1, the employees.group_id default

CREATE TABLE employees (
  id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  name         text NOT NULL,
  pin_username text NOT NULL UNIQUE CHECK (pin_username ~ '^[0-9]+$'),
  -- Only a bcrypt hash (R23.1): the owner types rows by hand, and a plain PIN must never be stored. Checked here, not in
  -- the model, since a failing validator would echo the PIN in its error.
  pin_hash     text NOT NULL CHECK (pin_hash ~ '^[$]2[abxy][$][0-9]{2}[$][./A-Za-z0-9]{53}$'),
  role         employee_role NOT NULL DEFAULT 'employee',
  active       boolean NOT NULL DEFAULT true,
  group_id     bigint NOT NULL DEFAULT 1 REFERENCES groups (id),
  updated_at   timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER employees_updated_at BEFORE UPDATE ON employees FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- db_login is filled by the database from the inserting login; the clocks' column grants leave it out, so it
-- identifies the clock without trusting its INSTANCE_ID (R26.2).
CREATE TABLE fingerprints (
  id            uuid PRIMARY KEY,
  employee_id   bigint NOT NULL REFERENCES employees (id) ON DELETE CASCADE,
  template      bytea NOT NULL,
  registered_at timestamptz NOT NULL,
  instance_id   text NOT NULL,
  db_login      text NOT NULL DEFAULT session_user
);

-- Append-only to the clocks (insert-only grants); the owner's `postgres` account may still fix rows (R38.1).
CREATE TABLE punches (
  id          uuid PRIMARY KEY,
  employee_id bigint NOT NULL REFERENCES employees (id),
  kind        punch_kind NOT NULL,
  at          timestamptz NOT NULL,
  instance_id text NOT NULL,
  automatic   boolean NOT NULL DEFAULT false,
  received_at timestamptz NOT NULL DEFAULT now(),
  db_login    text NOT NULL DEFAULT session_user
);
CREATE INDEX punches_employee_at ON punches (employee_id, at);
CREATE INDEX punches_at ON punches (at);

CREATE TABLE punch_corrections (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  punch_id    uuid NOT NULL REFERENCES punches (id),
  action      correction_action NOT NULL,
  new_at      timestamptz,
  new_kind    punch_kind,
  note        text NOT NULL DEFAULT '',
  recorded_at timestamptz NOT NULL DEFAULT now(),
  CHECK ((action = 'void' AND new_at IS NULL AND new_kind IS NULL)
      OR (action = 'amend' AND (new_at IS NOT NULL OR new_kind IS NOT NULL)))
);
CREATE INDEX punch_corrections_punch ON punch_corrections (punch_id);

CREATE TABLE overrides (
  id          uuid PRIMARY KEY,
  punch_id    uuid NOT NULL REFERENCES punches (id),
  employee_id bigint NOT NULL REFERENCES employees (id),
  manager_id  bigint NOT NULL REFERENCES employees (id),
  reason      override_reason NOT NULL,
  pin_only    boolean NOT NULL,
  at          timestamptz NOT NULL,
  instance_id text NOT NULL,
  received_at timestamptz NOT NULL DEFAULT now(),
  db_login    text NOT NULL DEFAULT session_user
);

-- updated_at lets the clock alert once per bad row version (§4.6).
CREATE TABLE rules (
  id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  kind         text NOT NULL,
  -- A non-object would fail to load into `RuleRow` at all; as an object every row loads, and a bad one fails
  -- `validate_rule` (skip and alert, §4.6).
  params       jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(params) = 'object'),
  group_id     bigint REFERENCES groups (id),
  employee_id  bigint REFERENCES employees (id),
  on_violation rule_action NOT NULL,
  updated_at   timestamptz NOT NULL DEFAULT now(),
  CHECK (num_nonnulls(group_id, employee_id) = 1)
);
CREATE TRIGGER rules_updated_at BEFORE UPDATE ON rules FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE SCHEMA config;

-- Seeded empty: a new install's reader sleeps until hours are added (§4.3).
-- Here and in config.settings, `time` accepts '24:00' but psycopg can't load it (Python's `time` has no hour 24), and
-- one such row breaks every clock's catch-up; midnight is written '23:59:59'.
CREATE TABLE config.business_hours (
  id      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  weekday smallint NOT NULL CHECK (weekday BETWEEN 1 AND 7),
  opens   time NOT NULL,
  closes  time NOT NULL,
  CHECK (opens < closes AND closes < '24:00')
);

CREATE TABLE config.settings (
  id                        boolean PRIMARY KEY DEFAULT true CHECK (id),
  auto_clock_out_enabled    boolean NOT NULL DEFAULT true,
  auto_clock_out_run_time   time NOT NULL DEFAULT '23:00:00',
  auto_clock_out_stamp_time time NOT NULL DEFAULT '23:00:00',
  subtract_lunch            boolean NOT NULL DEFAULT true,
  subtract_breaks           boolean NOT NULL DEFAULT false,
  auto_clock_out_frequency  auto_clock_out_frequency NOT NULL DEFAULT 'daily',
  -- Business-wide switches for starting a lunch or a break (the clock always offers ending one); a per-group override may
  -- later take precedence.
  lunches_enabled           boolean NOT NULL DEFAULT true,
  breaks_enabled            boolean NOT NULL DEFAULT true,
  CHECK (auto_clock_out_run_time < '24:00' AND auto_clock_out_stamp_time < '24:00')
);
INSERT INTO config.settings DEFAULT VALUES;

CREATE VIEW punch_log AS
SELECT p.id, p.employee_id, e.name AS employee_name, p.kind, p.at, p.instance_id, p.automatic, p.received_at,
       p.db_login, c.action AS correction, c.new_at, c.new_kind, c.note AS correction_note, c.recorded_at AS corrected_at
FROM punches p
JOIN employees e ON e.id = p.employee_id
LEFT JOIN LATERAL (
  SELECT pc.action, pc.new_at, pc.new_kind, pc.note, pc.recorded_at
  FROM punch_corrections pc
  WHERE pc.punch_id = p.id
  ORDER BY pc.recorded_at DESC, pc.id DESC
  LIMIT 1
) c ON true;

-- Payloads carry only the row's id as text, never the row: a 3.3 KB template would approach NOTIFY's 8,000-byte
-- limit (§4.4). Tables outside `public` are named `config.<table>`.
CREATE FUNCTION notify_change() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  row_id text;
BEGIN
  IF TG_OP = 'DELETE' THEN
    row_id := OLD.id::text;
  ELSE
    row_id := NEW.id::text;
  END IF;
  PERFORM pg_notify('timeclock', json_build_object(
    'table', CASE WHEN TG_TABLE_SCHEMA = 'public' THEN TG_TABLE_NAME ELSE TG_TABLE_SCHEMA || '.' || TG_TABLE_NAME END,
    'op', TG_OP,
    'id', row_id)::text);
  RETURN NULL;
END $$;

CREATE TRIGGER employees_notify AFTER INSERT OR UPDATE OR DELETE ON employees FOR EACH ROW EXECUTE FUNCTION notify_change();
CREATE TRIGGER groups_notify AFTER INSERT OR UPDATE OR DELETE ON groups FOR EACH ROW EXECUTE FUNCTION notify_change();
CREATE TRIGGER fingerprints_notify AFTER INSERT OR UPDATE OR DELETE ON fingerprints FOR EACH ROW EXECUTE FUNCTION notify_change();
CREATE TRIGGER punches_notify AFTER INSERT OR UPDATE OR DELETE ON punches FOR EACH ROW EXECUTE FUNCTION notify_change();
CREATE TRIGGER punch_corrections_notify AFTER INSERT OR UPDATE OR DELETE ON punch_corrections FOR EACH ROW EXECUTE FUNCTION notify_change();
CREATE TRIGGER rules_notify AFTER INSERT OR UPDATE OR DELETE ON rules FOR EACH ROW EXECUTE FUNCTION notify_change();
CREATE TRIGGER business_hours_notify AFTER INSERT OR UPDATE OR DELETE ON config.business_hours FOR EACH ROW EXECUTE FUNCTION notify_change();
CREATE TRIGGER settings_notify AFTER INSERT OR UPDATE OR DELETE ON config.settings FOR EACH ROW EXECUTE FUNCTION notify_change();

-- The automatic clock-out's run record (owner's design, 2026-10-27). One row per scheduled run, projected 7 days ahead in
-- the backend's zone; a `config.settings` edit or a change of that zone reprojects in the same transaction, and the
-- backend's every pass extends it as the days pass. A row that has run, or whose time has passed, is history and never
-- changes. Schema `backend` has no grant to `timeclock_clock_access` (R26.2).
CREATE SCHEMA backend;

-- The backend's `TZ`, written at every start; nothing is projected before the first.
CREATE TABLE backend.environment (
  id boolean PRIMARY KEY DEFAULT true CHECK (id),
  tz text    NOT NULL
);

-- scheduled_for and stamp are instants resolved from wall times in scheduled_tz. ran_at (when the run committed) and closed
-- (its clock-out count) are set together, in the run's own transaction.
CREATE TABLE backend.auto_clock_out_runs (
  id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  scheduled_for timestamptz NOT NULL,
  scheduled_tz  text NOT NULL,
  stamp         timestamptz NOT NULL,
  ran_at        timestamptz,
  closed        integer CHECK (closed >= 0),
  CHECK (stamp <= scheduled_for),
  CHECK ((ran_at IS NULL) = (closed IS NULL))
);
CREATE INDEX auto_clock_out_runs_scheduled_for ON backend.auto_clock_out_runs (scheduled_for);

-- `wall` in `run_zone` as an instant, by the owner's DST rule: a skipped wall time resolves to the gap's end (02:30 in a
-- 02:00-03:00 gap runs at 03:00), a repeated one to its first occurrence. `AT TIME ZONE` alone does the opposite of both
-- (skipped 02:30 -> 03:30, repeated -> the second). Independent of the session's TimeZone: hours, not days, are added to
-- timestamptz values.
CREATE FUNCTION backend.resolve_local_time(wall timestamp, run_zone text) RETURNS timestamptz LANGUAGE plpgsql STABLE AS $$
DECLARE
  -- `wall` under the UTC offset in force a day earlier and a day later; no zone changes offset twice within two days.
  earlier timestamptz := ((wall - interval '1 day') AT TIME ZONE run_zone) + interval '24 hours';
  later   timestamptz := ((wall + interval '1 day') AT TIME ZONE run_zone) - interval '24 hours';
  lo      bigint;
  hi      bigint;
  mid     bigint;
BEGIN
  IF (earlier AT TIME ZONE run_zone) = wall THEN
    RETURN earlier;  -- an ordinary time, or a repeated one's first occurrence (the earlier offset is the larger one)
  ELSIF (later AT TIME ZONE run_zone) = wall THEN
    RETURN later;
  END IF;
  -- Skipped: the gap ends at the first instant whose wall time reaches `wall`. `later` reads before the gap, `earlier` after
  -- it; bisect the whole seconds between (transitions fall on whole seconds).
  lo := extract(epoch FROM later);
  hi := extract(epoch FROM earlier);
  WHILE hi - lo > 1 LOOP
    mid := (lo + hi) / 2;
    IF (to_timestamp(mid) AT TIME ZONE run_zone) >= wall THEN
      hi := mid;
    ELSE
      lo := mid;
    END IF;
  END LOOP;
  RETURN to_timestamp(hi);
END $$;

-- What every clock login may do (R26.2), through the backend's group roles (§4.4, §8.3).
DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'timeclock_clock_access') THEN
    CREATE ROLE timeclock_clock_access NOLOGIN;
  END IF;
END $$;
GRANT USAGE ON SCHEMA config TO timeclock_clock_access;
GRANT SELECT ON employees, groups, fingerprints, punches, punch_corrections, rules, config.business_hours, config.settings
  TO timeclock_clock_access;
-- Column grants leave out db_login, received_at and punches.automatic: the database sets the first two, and only the
-- backend makes automatic clock-outs. SELECT (id) on overrides lets `ON CONFLICT (id) DO NOTHING` infer its target.
GRANT INSERT (id, employee_id, template, registered_at, instance_id) ON fingerprints TO timeclock_clock_access;
GRANT INSERT (id, employee_id, kind, at, instance_id) ON punches TO timeclock_clock_access;
GRANT INSERT (id, punch_id, employee_id, manager_id, reason, pin_only, at, instance_id) ON overrides TO timeclock_clock_access;
GRANT SELECT (id) ON overrides TO timeclock_clock_access;
