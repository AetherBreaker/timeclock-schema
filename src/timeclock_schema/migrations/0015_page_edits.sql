-- timeclock-schema 0015: what a badge (0014) may do to punches, and the block on an account's own (spec 2026-11-05 §4,
-- §6.10, §14). The database stamps who wrote each row from current_user, since the page SET ROLEs to a badge; clocks,
-- the owner and the backend never switch roles, so their stamps don't change.

-- One row per save from the page, holding its note; the punches and corrections it wrote point at it.
CREATE TABLE edits (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  note        text NOT NULL DEFAULT '',
  db_login    text NOT NULL DEFAULT current_user,
  recorded_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE punches ADD COLUMN edit_id uuid REFERENCES edits (id), ALTER COLUMN db_login SET DEFAULT current_user;

-- Existing corrections were all made by hand as the owner's postgres login. Pinned literally rather than taken from
-- current_user, which here would be whoever runs the migration.
ALTER TABLE punch_corrections
  ADD COLUMN db_login text NOT NULL DEFAULT 'postgres',
  ADD COLUMN edit_id uuid REFERENCES edits (id);
ALTER TABLE punch_corrections ALTER COLUMN db_login SET DEFAULT current_user;

-- The employee whose punches `who` may not change, or NULL for none. It takes `who` because current_user inside a
-- SECURITY DEFINER function is its owner. Superusers (the owner, the backend) are never blocked. The test database's
-- allowlist is never read (§3.2), so there the page supplies the signed-in account's number in
-- timeclock.editor_employee_id. Production ignores that setting and refuses a badge with no row, such as one a session
-- still holds after its row is deleted.
CREATE FUNCTION backend.editor_employee_id(who name) RETURNS bigint
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
DECLARE
  employee bigint;
BEGIN
  IF (SELECT r.rolsuper FROM pg_roles r WHERE r.rolname = who) THEN
    RETURN NULL;
  END IF;
  IF NOT coalesce((SELECT d.production FROM backend.deployment d), true) THEN
    RETURN nullif(current_setting('timeclock.editor_employee_id', true), '')::bigint;
  END IF;
  SELECT e.employee_id INTO employee FROM backend.editors e WHERE e.email = who::text;
  IF NOT FOUND THEN
    RAISE insufficient_privilege USING MESSAGE = 'this account is not on the allowlist';
  END IF;
  RETURN employee;
END $$;
REVOKE EXECUTE ON FUNCTION backend.editor_employee_id(name) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION backend.editor_employee_id(name) TO timeclock_editor_access;

CREATE FUNCTION refuse_own_punch() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.employee_id = backend.editor_employee_id(current_user) THEN
    RAISE insufficient_privilege USING MESSAGE = 'you can''t change your own punches';
  END IF;
  RETURN NEW;
END $$;

CREATE FUNCTION refuse_own_correction() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF (SELECT p.employee_id FROM punches p WHERE p.id = NEW.punch_id) = backend.editor_employee_id(current_user) THEN
    RAISE insufficient_privilege USING MESSAGE = 'you can''t change your own punches';
  END IF;
  RETURN NEW;
END $$;

-- The WHEN keeps clocks from calling the function at all; superusers count as members, pass it, and get NULL back.
CREATE TRIGGER punches_refuse_own BEFORE INSERT ON punches FOR EACH ROW
  WHEN (pg_has_role(current_user, 'timeclock_editor_access', 'MEMBER')) EXECUTE FUNCTION refuse_own_punch();
CREATE TRIGGER punch_corrections_refuse_own BEFORE INSERT ON punch_corrections FOR EACH ROW
  WHEN (pg_has_role(current_user, 'timeclock_editor_access', 'MEMBER')) EXECUTE FUNCTION refuse_own_correction();

-- Insert-only, as for the clocks: column grants leave out db_login, received_at and automatic, which the database or
-- the backend set. Fingerprint templates stay out of reach.
GRANT USAGE ON SCHEMA config, backend TO timeclock_editor_access;
GRANT SELECT ON employees, groups, punches, punch_corrections, edits, config.settings, config.business_hours,
  backend.environment TO timeclock_editor_access;
GRANT SELECT (id, employee_id, finger, registered_at, instance_id, db_login) ON fingerprints TO timeclock_editor_access;
GRANT INSERT (id, employee_id, kind, at, instance_id, edit_id) ON punches TO timeclock_editor_access;
GRANT INSERT (id, punch_id, action, new_at, new_kind, note, edit_id) ON punch_corrections TO timeclock_editor_access;
GRANT INSERT (id, note) ON edits TO timeclock_editor_access;
