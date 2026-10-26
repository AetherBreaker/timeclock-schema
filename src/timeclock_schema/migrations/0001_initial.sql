-- timeclock-schema 0001: every table, view, trigger and grant of design §4. Plain SQL: runs unchanged in
-- `timeclock` and `timeclock_test` (R26.6). Roles are server-wide, so the one role is created only if absent.

CREATE EXTENSION IF NOT EXISTS pgcrypto;  -- crypt()/gen_salt('bf') for PIN hashes (R23.1)

CREATE TYPE punch_kind AS ENUM ('clock_in', 'clock_out', 'break_start', 'break_end', 'lunch_start', 'lunch_end');
CREATE TYPE correction_action AS ENUM ('void', 'amend');
CREATE TYPE override_reason AS ENUM ('no_fingerprint', 'fingerprint_skipped');
CREATE TYPE rule_action AS ENUM ('refuse', 'warn', 'accept');

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
  pin_hash     text NOT NULL,
  is_manager   boolean NOT NULL DEFAULT false,
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
  params       jsonb NOT NULL DEFAULT '{}',
  group_id     bigint REFERENCES groups (id),
  employee_id  bigint REFERENCES employees (id),
  on_violation rule_action NOT NULL,
  updated_at   timestamptz NOT NULL DEFAULT now(),
  CHECK (num_nonnulls(group_id, employee_id) = 1)
);
CREATE TRIGGER rules_updated_at BEFORE UPDATE ON rules FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE SCHEMA config;

-- Seeded empty: a new install's reader sleeps until hours are added (§4.3).
CREATE TABLE config.business_hours (
  id      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  weekday smallint NOT NULL CHECK (weekday BETWEEN 1 AND 7),
  opens   time NOT NULL,
  closes  time NOT NULL,
  CHECK (opens < closes)
);

CREATE TABLE config.settings (
  id                        boolean PRIMARY KEY DEFAULT true CHECK (id),
  auto_clock_out_enabled    boolean NOT NULL DEFAULT true,
  auto_clock_out_run_time   time NOT NULL DEFAULT '23:00:00',
  auto_clock_out_stamp_time time NOT NULL DEFAULT '23:00:00',
  subtract_lunch            boolean NOT NULL DEFAULT true,
  subtract_breaks           boolean NOT NULL DEFAULT false
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
