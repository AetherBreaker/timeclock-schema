-- timeclock-schema 0011: a log of every fingerprint scan a clock makes (owner, 2026-11-04), to tune the match margin and
-- see why a scan matched, retried or failed; the `scan_log` view reads it. Two settings steer it: save_scan_images (whether
-- the clocks store the scan's image) and fingerprint_match_margin (how far the best score must beat the next employee's).

CREATE TYPE scan_result AS ENUM ('matched', 'no_match', 'too_close', 'retry');
CREATE TYPE scan_outcome AS ENUM ('punched', 'cancelled', 'timed_out', 'rescanned', 'other');

-- No foreign keys: a scan is a record of what the clock saw, kept as it was when an employee or print is later deleted or
-- replaced. best_* is the top-scoring print and other_* the best of a different employee; scores is every print's score
-- by fingerprint id. db_login is filled by the database, as on fingerprints.
CREATE TABLE fingerprint_scans (
  id                   uuid PRIMARY KEY,
  at                   timestamptz NOT NULL,
  instance_id          text NOT NULL,
  result               scan_result NOT NULL,
  best_fingerprint_id  uuid,
  best_employee_id     bigint,
  best_finger          finger,
  best_score           integer,
  other_fingerprint_id uuid,
  other_employee_id    bigint,
  other_finger         finger,
  other_score          integer,
  scores               jsonb NOT NULL DEFAULT '{}',
  margin               integer NOT NULL,
  outcome              scan_outcome,
  punch_id             uuid,
  retry_message        text,
  probe                bytea,
  image                bytea,
  image_width          integer,
  image_height         integer,
  received_at          timestamptz NOT NULL DEFAULT now(),
  db_login             text NOT NULL DEFAULT session_user
);
CREATE INDEX fingerprint_scans_at ON fingerprint_scans (at);

ALTER TABLE config.settings
  ADD COLUMN save_scan_images boolean NOT NULL DEFAULT true,
  ADD COLUMN fingerprint_match_margin integer NOT NULL DEFAULT 15 CHECK (fingerprint_match_margin >= 0);

-- No bytea columns: has_image says whether one is stored. Names are NULL, not '', where there is no employee. No grants:
-- only the owner reads it.
CREATE VIEW scan_log AS
SELECT s.at, s.instance_id, s.result,
       nullif(concat_ws(' ', e.first_name, e.middle_name, e.last_name), '') AS employee_name,
       s.best_finger AS finger, s.best_score AS score,
       nullif(concat_ws(' ', o.first_name, o.middle_name, o.last_name), '') AS other_name,
       s.other_finger, s.other_score, s.margin, s.outcome, p.kind AS punch_kind, s.retry_message,
       s.image IS NOT NULL AS has_image, s.scores, s.id
FROM fingerprint_scans s
LEFT JOIN employees e ON e.id = s.best_employee_id
LEFT JOIN employees o ON o.id = s.other_employee_id
LEFT JOIN punches p ON p.id = s.punch_id
ORDER BY s.at DESC;

-- Like fingerprint_bypasses: the clock adds rows and never reads or changes them; SELECT (id) lets `ON CONFLICT (id) DO
-- NOTHING` infer its target. No NOTIFY trigger: no clock reads another's scans.
GRANT INSERT (id, at, instance_id, result, best_fingerprint_id, best_employee_id, best_finger, best_score,
              other_fingerprint_id, other_employee_id, other_finger, other_score, scores, margin, outcome, punch_id,
              retry_message, probe, image, image_width, image_height) ON fingerprint_scans TO timeclock_clock_access;
GRANT SELECT (id) ON fingerprint_scans TO timeclock_clock_access;
