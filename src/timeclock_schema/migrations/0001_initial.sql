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

-- Prunes every unrun row later than `as_of` and projects the next 7 days in the backend's zone (`as_of` exists for tests;
-- callers pass nothing). `daily`, the only frequency: one row a local day from today to today + 6. A day already holding a
-- row (run, or missed and awaiting the backend's catch-up with its own stamp) gets none; today's row is projected even when
-- its time has passed, and the backend runs it at once. Nothing is projected without a settings row or a zone, while
-- disabled, or for a stamp time later than the run time (the backend alerts on that, R41.3). The advisory lock serializes
-- projections (an owner's edit, the backend's pass) so two can't both fill one day; runs don't take it.
-- DST is Postgres's own `AT TIME ZONE` (owner, 2026-10-27): a skipped wall time moves forward by the gap (02:30 -> 03:30),
-- a repeated one resolves to its second occurrence; neither depends on the session's TimeZone. A stamp skipped past its
-- run time (stamp 02:30, run 03:00 on spring-forward day) is capped at the run time: a clock-out is never in its run's future.
CREATE FUNCTION backend.project_auto_clock_out_runs(as_of timestamptz DEFAULT now()) RETURNS void LANGUAGE plpgsql AS $$
DECLARE
  s        config.settings%ROWTYPE;
  run_zone text;
  today    date;
  d        date;
  run_at   timestamptz;
BEGIN
  PERFORM pg_advisory_xact_lock(7461726503);
  DELETE FROM backend.auto_clock_out_runs WHERE ran_at IS NULL AND scheduled_for > as_of;
  SELECT * INTO s FROM config.settings;
  SELECT tz INTO run_zone FROM backend.environment;
  IF s.id IS NULL OR run_zone IS NULL OR NOT s.auto_clock_out_enabled
     OR s.auto_clock_out_stamp_time > s.auto_clock_out_run_time THEN
    RETURN;
  END IF;
  today := (as_of AT TIME ZONE run_zone)::date;
  FOR i IN 0..6 LOOP
    d := today + i;
    IF NOT EXISTS (SELECT FROM backend.auto_clock_out_runs r WHERE (r.scheduled_for AT TIME ZONE r.scheduled_tz)::date = d) THEN
      run_at := (d + s.auto_clock_out_run_time) AT TIME ZONE run_zone;
      INSERT INTO backend.auto_clock_out_runs (scheduled_for, scheduled_tz, stamp)
      VALUES (run_at, run_zone, LEAST((d + s.auto_clock_out_stamp_time) AT TIME ZONE run_zone, run_at));
    END IF;
  END LOOP;
END $$;

CREATE FUNCTION backend.reproject_auto_clock_out_runs() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  PERFORM backend.project_auto_clock_out_runs();
  RETURN NULL;
END $$;

-- Any settings change reprojects; an edit to an unrelated column reprojects identically. The backend upserts its zone
-- with a WHERE that updates nothing when it's unchanged, so only a new or changed zone fires the second trigger.
CREATE TRIGGER settings_reproject_auto_clock_out AFTER INSERT OR UPDATE OR DELETE ON config.settings
  FOR EACH ROW EXECUTE FUNCTION backend.reproject_auto_clock_out_runs();
CREATE TRIGGER environment_reproject_auto_clock_out AFTER INSERT OR UPDATE ON backend.environment
  FOR EACH ROW EXECUTE FUNCTION backend.reproject_auto_clock_out_runs();

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
