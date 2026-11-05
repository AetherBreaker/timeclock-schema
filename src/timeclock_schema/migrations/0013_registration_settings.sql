-- timeclock-schema 0013: the bars a clock's fingerprint registration judges presses by (owner, 2026-11-05). The clocks
-- read them live, like fingerprint_match_margin, so they're tuned on the test database, then set on production's without
-- a release. The defaults come from replaying the registration over the production prints of 2026-11-05.

ALTER TABLE config.settings
  ADD COLUMN registration_min_detail_points integer NOT NULL DEFAULT 20 CHECK (registration_min_detail_points >= 0),
  ADD COLUMN registration_max_detail_points integer NOT NULL DEFAULT 120,
  ADD COLUMN registration_centre_tolerance integer NOT NULL DEFAULT 100 CHECK (registration_centre_tolerance >= 0),
  ADD COLUMN registration_agreement_bar integer NOT NULL DEFAULT 41 CHECK (registration_agreement_bar >= 0),
  ADD COLUMN registration_agreement_needed integer NOT NULL DEFAULT 1 CHECK (registration_agreement_needed BETWEEN 0 AND 4),
  ADD COLUMN registration_resemblance_bar integer NOT NULL DEFAULT 40 CHECK (registration_resemblance_bar >= 0),
  ADD COLUMN registration_extra_presses integer NOT NULL DEFAULT 5 CHECK (registration_extra_presses >= 0),
  ADD CONSTRAINT registration_detail_points_range CHECK (registration_max_detail_points >= registration_min_detail_points);
