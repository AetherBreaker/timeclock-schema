-- timeclock-schema 0026: each scheduled report's recipients, which the backend reads at every run, so the list changes
-- without a release. `report` is the backend registry entry's name. Edited by hand, and only production's rows are
-- read, as backend.editors (0014); the backend's own login is the only user, so there are no grants (as 0021).
CREATE TABLE backend.report_recipients (
  report text NOT NULL,
  email  text NOT NULL CHECK (email = lower(email) AND email ~ E'^[^@\\s]+@[^@\\s]+[.][^@\\s]+$'),
  PRIMARY KEY (report, email)
);

-- The address the backend's code sent the payroll report to before this table, so the first run after it sends the same.
INSERT INTO backend.report_recipients VALUES ('payroll_report', 'jacob.ogden@sweetfiretobacco.com');
