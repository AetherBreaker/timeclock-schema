-- timeclock-schema 0023: how many breaks an employee is expected to take in a local day (owner, 2026-11-13). The shift
-- corrections page flags a shift holding a break past it, until the day's breaks are back within it or someone marks
-- those breaks as correct; it never blocks a punch or a save. 0 turns the flag off. Only the page reads it: a clock's
-- settings SELECT names its own columns.

ALTER TABLE config.settings ADD COLUMN max_breaks_per_day integer NOT NULL DEFAULT 2 CHECK (max_breaks_per_day >= 0);
