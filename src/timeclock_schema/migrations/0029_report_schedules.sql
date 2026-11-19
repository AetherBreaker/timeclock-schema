-- timeclock-schema 0029: each scheduled report's cron line, so its send time changes without a release (owner,
-- 2026-11-18). `report` is the backend registry entry's name. Edited by hand, and only production's rows are read, as
-- backend.report_recipients (0026); the backend's own login is the only user, so there are no grants.
CREATE TABLE backend.report_schedules (
  report text PRIMARY KEY,
  cron   text NOT NULL CHECK (btrim(cron) <> '')
);

INSERT INTO backend.report_schedules VALUES ('payroll_report', '0 13 * * TUE');  -- Tuesdays at 1 PM, New York time (owner)

-- A channel of its own, so the clocks never hear it; the backend re-reads every row, so the name is only a hint.
CREATE FUNCTION backend.ping_report_schedules() RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, pg_temp AS $$
BEGIN
  PERFORM pg_notify('report_schedules', CASE TG_OP WHEN 'DELETE' THEN OLD.report ELSE NEW.report END);
  RETURN NULL;
END $$;
CREATE TRIGGER report_schedules_ping AFTER INSERT OR UPDATE OR DELETE ON backend.report_schedules
  FOR EACH ROW EXECUTE FUNCTION backend.ping_report_schedules();
