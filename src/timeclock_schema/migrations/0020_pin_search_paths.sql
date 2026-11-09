-- timeclock-schema 0020: a session's temporary tables come before pg_catalog and public unless its search_path lists
-- pg_temp, so a clock allowed TEMPORARY could shadow what these read in its own insert: pg_roles in refuse_clock_admin
-- (0001), letting it add an admin, or fingerprint_scans in prune_scan_biometrics (0012), which runs as its owner, so the
-- prune misses the real table. The backend revokes TEMPORARY; this is the second line. Pinned as 0015's are, pg_temp last.
ALTER FUNCTION refuse_clock_admin() SET search_path = pg_catalog, public, pg_temp;
ALTER FUNCTION prune_scan_biometrics() SET search_path = pg_catalog, public, pg_temp;
