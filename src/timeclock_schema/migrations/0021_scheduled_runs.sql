-- timeclock-schema 0021: the backend records each catch-up scheduled job's last completed run here, so a run missed while
-- it was down is made up once at startup. Only production's copy is read. The backend's own login is the only user, so
-- there are no grants.
CREATE TABLE backend.scheduled_runs (
  name          text PRIMARY KEY,
  period        text NOT NULL,
  scheduled_for timestamptz NOT NULL,
  completed_at  timestamptz NOT NULL DEFAULT now()
);
