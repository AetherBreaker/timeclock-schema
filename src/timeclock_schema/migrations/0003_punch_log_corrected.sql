-- punch_log shows each punch as corrected, for the export to QuickBooks (owner, 2026-11-02): its newest correction (by
-- recorded_at, then id) wins, as in timeclock_schema.shifts; an amend's time and type replace whichever it sets, a void
-- leaves the punch out, and the log is in corrected-time order. Applied by hand to both databases that day; dropping and
-- recreating gives the same view either way. No grants: only the owner reads it.
DROP VIEW punch_log;
CREATE VIEW punch_log AS
SELECT p.id, p.employee_id, concat_ws(' ', e.first_name, e.middle_name, e.last_name) AS employee_name,
       coalesce(c.new_kind, p.kind) AS kind, coalesce(c.new_at, p.at) AS at, p.instance_id, p.automatic, p.received_at,
       p.db_login, c.note AS correction_note, c.recorded_at AS corrected_at
FROM punches p
JOIN employees e ON e.id = p.employee_id
LEFT JOIN LATERAL (
  SELECT pc.action, pc.new_at, pc.new_kind, pc.note, pc.recorded_at
  FROM punch_corrections pc
  WHERE pc.punch_id = p.id
  ORDER BY pc.recorded_at DESC, pc.id DESC
  LIMIT 1
) c ON true
WHERE c.action IS DISTINCT FROM 'void'
ORDER BY coalesce(c.new_at, p.at), p.id;
