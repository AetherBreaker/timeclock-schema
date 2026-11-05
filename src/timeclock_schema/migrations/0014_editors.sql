-- timeclock-schema 0014: the shift corrections page's allowlist, and a database role ("badge") for each allowlisted
-- account (spec 2026-11-05 §3, §4). The page signs people in with Zoho and lets in only the emails listed here. It
-- connects as timeclock_web, which holds no privileges, and does each request's work as the signed-in account's badge
-- (SET ROLE), so the database stamps rows with the email and holds the account to the badge's rules. Badges get their
-- privileges through timeclock_editor_access (0015, 0016).

DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'timeclock_web') THEN
    CREATE ROLE timeclock_web NOLOGIN;  -- the backend gives it LOGIN, its password and CONNECT
  END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'timeclock_editor_access') THEN
    CREATE ROLE timeclock_editor_access NOLOGIN;
  END IF;
END $$;

-- Edited by hand. Only production's rows are read (§3.2); the test database has the table but its rows do nothing. A
-- blank employee_id never matches an employee, so that account has no self-edit block. The email is the badge's name:
-- lowercase so Zoho's casing can't make a second badge, and at most 63 bytes since a longer name is silently truncated,
-- which could give two emails one badge.
CREATE TABLE backend.editors (
  email       text PRIMARY KEY
              CHECK (email = lower(email) AND octet_length(email) <= 63 AND email ~ E'^[^@\\s]+@[^@\\s]+[.][^@\\s]+$'),
  employee_id bigint REFERENCES employees (id),
  developer   boolean NOT NULL DEFAULT false
);

-- The backend writes production = false into timeclock_test. No row means production, so an unconfigured database
-- fails closed: it never trusts a value the page supplies (0015).
CREATE TABLE backend.deployment (
  id         boolean PRIMARY KEY DEFAULT true CHECK (id),
  production boolean NOT NULL
);

-- Creates, moves and drops each row's badge. Roles are server-wide, so outside production this does nothing: deleting a
-- row from the test database's unused list must never drop a production badge. It runs as its owner, so on any other
-- table it would grant or drop any role: nobody else may run it, and it refuses any table but this one.
CREATE FUNCTION backend.sync_editor_badge() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $$
BEGIN
  IF TG_RELID <> 'backend.editors'::regclass THEN
    RAISE EXCEPTION 'backend.sync_editor_badge() serves only backend.editors';
  END IF;
  IF NOT coalesce((SELECT d.production FROM backend.deployment d), true) THEN
    RETURN NULL;
  END IF;
  IF TG_OP = 'DELETE' OR (TG_OP = 'UPDATE' AND OLD.email <> NEW.email) THEN
    EXECUTE format('DROP ROLE IF EXISTS %I', OLD.email);
  END IF;
  IF TG_OP <> 'DELETE' THEN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = NEW.email) THEN
      EXECUTE format('CREATE ROLE %I NOLOGIN', NEW.email);
    END IF;
    -- SET FALSE: a badge can't become the group, which no allowlist row names.
    EXECUTE format('GRANT timeclock_editor_access TO %I WITH INHERIT TRUE, SET FALSE', NEW.email);
    -- INHERIT FALSE: the page login gains nothing until it SETs ROLE to the badge.
    EXECUTE format('GRANT %I TO timeclock_web WITH INHERIT FALSE, SET TRUE', NEW.email);
  END IF;
  RETURN NULL;
END $$;
REVOKE EXECUTE ON FUNCTION backend.sync_editor_badge() FROM PUBLIC;
CREATE TRIGGER editors_badge AFTER INSERT OR UPDATE OR DELETE ON backend.editors
  FOR EACH ROW EXECUTE FUNCTION backend.sync_editor_badge();

-- The page's one read before it picks a badge: whether an email may sign in, and as whom.
CREATE FUNCTION backend.editor_account(email text) RETURNS TABLE (employee_id bigint, developer boolean)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
  SELECT e.employee_id, e.developer FROM backend.editors e WHERE e.email = lower(editor_account.email)
$$;
REVOKE EXECUTE ON FUNCTION backend.editor_account(text) FROM PUBLIC;
GRANT USAGE ON SCHEMA backend TO timeclock_web;  -- to name the function; it still can't read a backend table
GRANT EXECUTE ON FUNCTION backend.editor_account(text) TO timeclock_web;
