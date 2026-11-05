-- timeclock-schema 0016: employee edits from the page's Employees menu, and a log of every employee change (spec
-- 2026-11-05 §9).

-- Employee rows change in place, so this keeps who changed what: one row per changed column, from any login. A PIN
-- change is logged as field 'pin' with no values, never the hash. ON UPDATE CASCADE so the owner can still renumber an
-- employee whose changes are logged.
CREATE TABLE employee_changes (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  employee_id bigint NOT NULL REFERENCES employees (id) ON DELETE CASCADE ON UPDATE CASCADE,
  field       text NOT NULL,
  old_value   text,
  new_value   text,
  edit_id     uuid REFERENCES edits (id),
  db_login    text NOT NULL DEFAULT current_user,
  recorded_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX employee_changes_employee ON employee_changes (employee_id, recorded_at);

-- SECURITY INVOKER, so db_login stamps the badge (0015); badges therefore get INSERT on the log. The page names its save
-- in timeclock.edit_id. search_path pinned as on 0015's triggers.
CREATE FUNCTION log_employee_changes() RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
  edit uuid := nullif(current_setting('timeclock.edit_id', true), '')::uuid;
  old_row jsonb := to_jsonb(OLD);
  new_row jsonb := to_jsonb(NEW);
BEGIN
  INSERT INTO employee_changes (employee_id, field, old_value, new_value, edit_id)
  SELECT NEW.id, k, old_row ->> k, new_row ->> k, edit
  FROM jsonb_object_keys(new_row) k
  WHERE k NOT IN ('id', 'updated_at', 'pin_hash') AND old_row -> k IS DISTINCT FROM new_row -> k;
  IF OLD.pin_hash IS DISTINCT FROM NEW.pin_hash THEN
    INSERT INTO employee_changes (employee_id, field, edit_id) VALUES (NEW.id, 'pin', edit);
  END IF;
  RETURN NULL;
END $$;
CREATE TRIGGER employees_log_changes AFTER UPDATE ON employees FOR EACH ROW EXECUTE FUNCTION log_employee_changes();

-- An editor may change their own name, contact details and PIN, but not these (owner). The lookup runs on every update
-- so a badge with no allowlist row is refused here too.
CREATE FUNCTION refuse_own_settings() RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
  own bigint := backend.editor_employee_id(current_user);
BEGIN
  IF OLD.id = own AND (NEW.role, NEW.group_id, NEW.fingerprint_exempt, NEW.active)
                      IS DISTINCT FROM (OLD.role, OLD.group_id, OLD.fingerprint_exempt, OLD.active) THEN
    RAISE insufficient_privilege
      USING MESSAGE = 'you can''t change your own role, group, fingerprint exemption or active status';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER employees_refuse_own_settings BEFORE UPDATE ON employees FOR EACH ROW
  WHEN (pg_has_role(current_user, 'timeclock_editor_access', 'MEMBER')) EXECUTE FUNCTION refuse_own_settings();

GRANT UPDATE (first_name, middle_name, last_name, phone, email, role, group_id, fingerprint_exempt, active, pin_hash)
  ON employees TO timeclock_editor_access;
GRANT SELECT ON employee_changes TO timeclock_editor_access;
GRANT INSERT (employee_id, field, old_value, new_value, edit_id) ON employee_changes TO timeclock_editor_access;
