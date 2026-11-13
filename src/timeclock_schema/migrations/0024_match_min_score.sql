-- timeclock-schema 0024: a minimum fingerprint match score (owner, 2026-11-13), after a weak press, barely ahead of the
-- runner-up, opened another employee's punch menu. A clock refuses a match whose best score is under
-- fingerprint_match_min_score and logs the scan as too_weak; 40 or less leaves the reader's own threshold (40) in charge.
-- The lead follows production's, raised from 15 to 20 the same day: the default for a new row, and the existing row's
-- value where it still holds the old default (the test database; a fresh one's row predates this).

ALTER TABLE config.settings ADD COLUMN fingerprint_match_min_score integer NOT NULL DEFAULT 45
  CHECK (fingerprint_match_min_score >= 0);
ALTER TABLE config.settings ALTER COLUMN fingerprint_match_margin SET DEFAULT 20;
UPDATE config.settings SET fingerprint_match_margin = 20 WHERE fingerprint_match_margin = 15;
ALTER TYPE scan_result ADD VALUE 'too_weak' AFTER 'too_close';
