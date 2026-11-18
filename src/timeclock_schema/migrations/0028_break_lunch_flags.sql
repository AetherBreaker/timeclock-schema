-- timeclock-schema 0028: when the shift corrections page flags a break or lunch as long (owner, 2026-11-18). A break
-- reaching break_limit_minutes + break_grace_minutes, or a lunch reaching lunch_expected_minutes + lunch_grace_minutes,
-- is flagged until it's corrected or marked as correct; it never blocks a punch or a save, and payroll never reads these.
-- Only the page reads them: a clock's settings SELECT names its own columns.

ALTER TABLE config.settings
  ADD COLUMN break_grace_minutes integer NOT NULL DEFAULT 15 CHECK (break_grace_minutes >= 0),
  ADD COLUMN lunch_expected_minutes integer NOT NULL DEFAULT 30 CHECK (lunch_expected_minutes >= 0),
  ADD COLUMN lunch_grace_minutes integer NOT NULL DEFAULT 15 CHECK (lunch_grace_minutes >= 0);
