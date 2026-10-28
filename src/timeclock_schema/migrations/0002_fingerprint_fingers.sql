-- Every print names its finger, one per finger per person; approved changes are logged; clocks may delete prints (the
-- fingerprint menu, 2026-10-28).

-- `finger` is NOT NULL with no default: an existing print can't be labelled here, so the owner labels or deletes them.
DO $$
BEGIN
  IF EXISTS (SELECT FROM fingerprints) THEN
    RAISE EXCEPTION '0002 needs an empty fingerprints table: label or delete the existing prints first';
  END IF;
END
$$;

CREATE TYPE finger AS ENUM (
  'left_thumb', 'left_index', 'left_middle', 'left_ring', 'left_little',
  'right_thumb', 'right_index', 'right_middle', 'right_ring', 'right_little'
);
CREATE TYPE fingerprint_change_action AS ENUM ('add', 'replace', 'remove');

ALTER TABLE fingerprints
  ADD COLUMN finger finger NOT NULL,
  ADD CONSTRAINT fingerprints_one_per_finger UNIQUE (employee_id, finger);

-- Append-only to the clocks, like punches. No notify trigger: no clock reads it.
CREATE TABLE fingerprint_changes (
  id          uuid PRIMARY KEY,
  employee_id bigint NOT NULL REFERENCES employees (id),
  finger      finger NOT NULL,
  action      fingerprint_change_action NOT NULL,
  approved_by bigint REFERENCES employees (id),
  pin_only    boolean,
  at          timestamptz NOT NULL,
  instance_id text NOT NULL,
  received_at timestamptz NOT NULL DEFAULT now(),
  db_login    text NOT NULL DEFAULT session_user,
  CHECK ((approved_by IS NULL) = (pin_only IS NULL)),
  -- Only a first registration (automatic, or the add-employee flow) goes unapproved.
  CHECK (action = 'add' OR approved_by IS NOT NULL)
);

GRANT INSERT (finger) ON fingerprints TO timeclock_clock_access;
-- A removal or re-register; the notify trigger already covers DELETE, so other clocks follow.
GRANT DELETE ON fingerprints TO timeclock_clock_access;
GRANT INSERT (id, employee_id, finger, action, approved_by, pin_only, at, instance_id) ON fingerprint_changes
  TO timeclock_clock_access;
-- ON CONFLICT (id) DO NOTHING needs to read the conflicting id.
GRANT SELECT (id) ON fingerprint_changes TO timeclock_clock_access;
