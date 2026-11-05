-- timeclock-schema 0017: a punch added on the shift corrections page (0015) carries its save's note and time where a
-- correction's would go (spec 2026-11-05 §14), so punch_log shows the note and shift_summary, whose corrected already
-- tests corrected_at, counts it as changed by hand. A correction's note and time still win: c.note is NULL only when
-- the punch has none (the column is NOT NULL). Otherwise as 0007; punch_log, weekly_hours, daily_hours and
-- shift_summary read this function and pick the change up unchanged.
CREATE OR REPLACE FUNCTION counted_punches()
RETURNS TABLE (id uuid, employee_id bigint, kind punch_kind, at timestamptz, instance_id text, automatic boolean,
               received_at timestamptz, db_login text, correction_note text, corrected_at timestamptz)
LANGUAGE plpgsql STABLE AS $$
DECLARE
  state text;  -- off, working, break or lunch
BEGIN
  FOR employee_id IN SELECT DISTINCT pu.employee_id FROM punches pu LOOP
    state := 'off';
    FOR id, kind, at, instance_id, automatic, received_at, db_login, correction_note, corrected_at IN
      SELECT pu.id, coalesce(c.new_kind, pu.kind), coalesce(c.new_at, pu.at), pu.instance_id, pu.automatic, pu.received_at,
             pu.db_login, coalesce(c.note, pe.note), coalesce(c.recorded_at, pe.recorded_at)
      FROM punches pu
      LEFT JOIN edits pe ON pe.id = pu.edit_id
      LEFT JOIN LATERAL (
        SELECT pc.action, pc.new_at, pc.new_kind, pc.note, pc.recorded_at
        FROM punch_corrections pc
        WHERE pc.punch_id = pu.id
        ORDER BY pc.recorded_at DESC, pc.id DESC
        LIMIT 1
      ) c ON true
      WHERE pu.employee_id = counted_punches.employee_id AND c.action IS DISTINCT FROM 'void'
      ORDER BY coalesce(c.new_at, pu.at), pu.id
    LOOP
      CONTINUE WHEN NOT (state = 'off' AND kind = 'clock_in'
        OR state = 'working' AND kind IN ('clock_out', 'break_start', 'lunch_start')
        OR state IN ('break', 'lunch') AND kind::text IN (state || '_end', 'clock_out'));
      state := CASE WHEN kind = 'clock_out' THEN 'off'
                    WHEN kind IN ('break_start', 'lunch_start') THEN split_part(kind::text, '_', 1)
                    ELSE 'working' END;
      RETURN NEXT;
    END LOOP;
  END LOOP;
END $$;
REVOKE EXECUTE ON FUNCTION counted_punches() FROM PUBLIC;
