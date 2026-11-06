-- timeclock-schema 0019: who added each employee, for the page's technical details (spec 2026-11-05 §11). Rows from
-- before stay NULL: nothing recorded it. current_user, as on the page's tables (0015), though clocks and DBeaver never
-- switch roles; nothing updates it afterwards.
ALTER TABLE employees ADD COLUMN db_login text DEFAULT current_user;

GRANT SELECT (db_login) ON employees TO timeclock_editor_access;
