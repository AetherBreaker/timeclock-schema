-- timeclock-schema 0027: a developer's request to send a report's week now, to addresses they pick from its enrolled
-- list (backend.report_recipients, 0026). The page inserts the request as the developer's badge; the backend, pinged on
-- `report_sends`, sends it and writes the outcome back. Developers are the badges holding timeclock_report_senders.

DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'timeclock_report_senders') THEN
    CREATE ROLE timeclock_report_senders NOLOGIN;  -- server-wide, so production's grants hold in the test database too
  END IF;
END $$;

CREATE TABLE backend.report_sends (
  id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  report       text NOT NULL,  -- the backend registry entry's name
  week_start   date NOT NULL CHECK (extract(isodow FROM week_start) = 1),
  recipients   text[] NOT NULL,
  requested_by text NOT NULL DEFAULT current_user,
  requested_at timestamptz NOT NULL DEFAULT now(),
  status       text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'sending', 'sent', 'failed')),
  finished_at  timestamptz,
  error        text
);

-- Runs as the inserting login, so current_user is the badge (as 0015 stamps db_login); a SECURITY DEFINER function would
-- stamp its owner. Enforced for every login, the owner's too, so only enrolled addresses can ever be requested.
CREATE FUNCTION backend.check_report_send() RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, pg_temp AS $$
DECLARE
  missing text;
BEGIN
  NEW.requested_by := current_user;
  NEW.requested_at := now();
  NEW.status := 'pending';
  NEW.finished_at := NULL;
  NEW.error := NULL;
  IF coalesce(cardinality(NEW.recipients), 0) = 0 THEN
    RAISE EXCEPTION 'a report send needs at least one recipient';
  END IF;
  IF cardinality(NEW.recipients) <> (SELECT count(DISTINCT r) FROM unnest(NEW.recipients) r) THEN
    RAISE EXCEPTION 'a report send lists a recipient twice';
  END IF;
  SELECT r INTO missing FROM unnest(NEW.recipients) r
  WHERE NOT EXISTS (SELECT FROM backend.report_recipients e WHERE e.report = NEW.report AND e.email = r) LIMIT 1;
  IF missing IS NOT NULL THEN
    RAISE EXCEPTION '% is not enrolled for %', missing, NEW.report;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER report_sends_check BEFORE INSERT ON backend.report_sends
  FOR EACH ROW EXECUTE FUNCTION backend.check_report_send();

-- A channel of its own, so the clocks (on `timeclock`) never hear it. NOTIFY needs no privilege: a ping only means "look".
CREATE FUNCTION backend.ping_report_sends() RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, pg_temp AS $$
BEGIN
  PERFORM pg_notify('report_sends', NEW.id::text);
  RETURN NULL;
END $$;
CREATE TRIGGER report_sends_ping AFTER INSERT ON backend.report_sends
  FOR EACH ROW EXECUTE FUNCTION backend.ping_report_sends();

-- 0014's badge sync, now also granting timeclock_report_senders to a developer's badge and taking it from anyone else's.
CREATE OR REPLACE FUNCTION backend.sync_editor_badge() RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
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
    IF NEW.developer THEN
      EXECUTE format('GRANT timeclock_report_senders TO %I WITH INHERIT TRUE, SET FALSE', NEW.email);
    ELSIF pg_has_role(NEW.email, 'timeclock_report_senders', 'MEMBER') THEN
      EXECUTE format('REVOKE timeclock_report_senders FROM %I', NEW.email);
    END IF;
    -- INHERIT FALSE: the page login gains nothing until it SETs ROLE to the badge.
    EXECUTE format('GRANT %I TO timeclock_web WITH INHERIT FALSE, SET TRUE', NEW.email);
  END IF;
  RETURN NULL;
END $$;

-- Today's developers, in production only, as the trigger does.
DO $$
DECLARE
  developer text;
BEGIN
  IF coalesce((SELECT d.production FROM backend.deployment d), true) THEN
    FOR developer IN SELECT e.email FROM backend.editors e WHERE e.developer AND EXISTS (SELECT FROM pg_roles WHERE rolname = e.email) LOOP
      EXECUTE format('GRANT timeclock_report_senders TO %I WITH INHERIT TRUE, SET FALSE', developer);
    END LOOP;
  END IF;
END $$;

GRANT USAGE ON SCHEMA backend TO timeclock_report_senders;
GRANT SELECT ON backend.report_recipients, backend.report_sends TO timeclock_report_senders;
GRANT INSERT (report, week_start, recipients) ON backend.report_sends TO timeclock_report_senders;
