-- timeclock-schema 0012: the scan log keeps fingerprint data 28 days (owner, 2026-11-04). Each insert into
-- fingerprint_scans clears the probe template and image of older scans, a late clock's own included; the rest of each row
-- stays. A trigger, not a scheduled job: it's one migration, and images only arrive with inserts.

-- Only rows still holding data, so a prune reads just what it clears, not the whole log.
CREATE INDEX fingerprint_scans_biometrics_at ON fingerprint_scans (at) WHERE probe IS NOT NULL OR image IS NOT NULL;

-- SECURITY DEFINER: it runs inside a clock's insert, and the clocks may only insert scans. The try-lock skips a prune when
-- another insert's is under way, so two clocks' inserts never wait on or deadlock over the same old rows; the next insert
-- prunes instead. Distinct from the backend's keys (7461726501, 7461726502) and 0001's projection lock (7461726503).
CREATE FUNCTION prune_scan_biometrics() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
BEGIN
  IF pg_try_advisory_xact_lock(7461726504) THEN
    UPDATE fingerprint_scans SET probe = NULL, image = NULL, image_width = NULL, image_height = NULL
    WHERE at < now() - interval '28 days' AND (probe IS NOT NULL OR image IS NOT NULL);
  END IF;
  RETURN NULL;
END $$;

CREATE TRIGGER fingerprint_scans_prune AFTER INSERT ON fingerprint_scans
FOR EACH STATEMENT EXECUTE FUNCTION prune_scan_biometrics();
