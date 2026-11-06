-- timeclock-schema 0018: a registration now gives up only on presses that disagree with the others; a press failing its
-- own checks (placement, pressure, a wet finger) is asked again without limit (owner, 2026-11-06). The setting counts
-- disagreeing presses alone, so it's named for them. Its constraints keep the column's name; column privileges follow it.

ALTER TABLE config.settings RENAME COLUMN registration_extra_presses TO registration_disagreement_retries;
ALTER TABLE config.settings
  RENAME CONSTRAINT settings_registration_extra_presses_check TO settings_registration_disagreement_retries_check;
ALTER TABLE config.settings
  RENAME CONSTRAINT settings_registration_extra_presses_not_null TO settings_registration_disagreement_retries_not_null;
