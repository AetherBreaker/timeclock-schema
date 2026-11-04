-- punch_log shows only the punches the hours count (owner, 2026-11-04): a punch not valid from the employee's state,
-- such as the later of two clock-ins in a row, is left out, as weekly_hours leaves it out. counted_punches() walks each
-- employee's punches as corrected through the state machine once, for both views; weekly_hours then pairs them up and
-- punch_intervals() goes. Neither view reads the other: the backend reruns 0003-0006 on the databases they were
-- applied to by hand, and 0003 drops punch_log. Applied by hand to both databases first, hence OR REPLACE.

-- As timeclock_schema.shifts.build_timeline: each punch's newest correction (by recorded_at, then id) applies, a void
-- drops it, and the rest are walked in (at, id) order; a punch not valid from the current state is ignored. Every
-- column is qualified: the output columns share the tables' names.
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
             pu.db_login, c.note, c.recorded_at
      FROM punches pu
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

CREATE OR REPLACE VIEW punch_log AS
SELECT c.id, c.employee_id, concat_ws(' ', e.first_name, e.middle_name, e.last_name) AS employee_name, c.kind, c.at,
       c.instance_id, c.automatic, c.received_at, c.db_login, c.correction_note, c.corrected_at
FROM counted_punches() c
JOIN employees e ON e.id = c.employee_id
ORDER BY c.at, c.id;

CREATE OR REPLACE VIEW weekly_hours AS
-- Lookups, not a join: the two one-row tables have no statistics, so a join of them is estimated at 1.5 million rows and
-- the planner JIT-compiles the query (300 ms, against 8 without).
WITH env AS (
  SELECT (SELECT tz FROM backend.environment) AS tz,
         (SELECT paid_break_minutes FROM config.settings) * interval '1 minute' AS allowance
),
-- The punches counted so far (one stamped after now(), such as an early automatic clock-out, doesn't count yet), each
-- with the next one and the next clock-out.
n AS (
  SELECT c.employee_id, c.kind, c.at, lead(c.at) OVER w AS next_at,
         min(c.at) FILTER (WHERE c.kind = 'clock_out') OVER (w ROWS BETWEEN 1 FOLLOWING AND UNBOUNDED FOLLOWING) AS next_out
  FROM counted_punches() c
  WHERE c.at <= now()
  WINDOW w AS (PARTITION BY c.employee_id ORDER BY c.at, c.id)
),
-- A shift runs from its clock-in to the next clock-out, a break or lunch from its start to the next punch (its end, or
-- a clock-out); one still open runs to now().
i AS (
  SELECT n.employee_id, CASE n.kind WHEN 'clock_in' THEN 'shift' WHEN 'break_start' THEN 'break' ELSE 'lunch' END AS kind,
         n.at AS starts, coalesce(CASE n.kind WHEN 'clock_in' THEN n.next_out ELSE n.next_at END, now()) AS ends
  FROM n
  WHERE n.kind IN ('clock_in', 'break_start', 'lunch_start')
),
-- The intervals, plus each break's paid head: the length its local day's allowance still covers after that day's earlier
-- breaks. A week edge then clips the head and the break alike, never changing how much of the break is paid.
spans AS (
  SELECT i.employee_id, i.kind, i.starts, i.ends FROM i
  UNION ALL
  SELECT i.employee_id, 'paid', i.starts,
         i.starts + greatest(least(i.ends - i.starts, env.allowance - coalesce(sum(i.ends - i.starts) OVER (
           PARTITION BY i.employee_id, (i.starts AT TIME ZONE env.tz)::date
           ORDER BY i.starts ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING), '0')), '0')
  FROM i CROSS JOIN env WHERE i.kind = 'break'
),
-- Each span's part in each week it touches; a span ending at Monday 00:00 touches that week with nothing, left out.
parts AS (
  SELECT s.employee_id, s.kind, w.monday::date AS week_of,
         least(s.ends, (w.monday + interval '1 week') AT TIME ZONE env.tz) - greatest(s.starts, w.monday AT TIME ZONE env.tz) AS length
  FROM spans s CROSS JOIN env
  CROSS JOIN generate_series(date_trunc('week', s.starts AT TIME ZONE env.tz), date_trunc('week', s.ends AT TIME ZONE env.tz),
                             interval '1 week') w (monday)
),
totals AS (
  SELECT employee_id, week_of,
         sum(CASE WHEN kind = 'shift' THEN length WHEN kind <> 'paid' THEN -length END) AS worked,
         coalesce(sum(length) FILTER (WHERE kind = 'paid'), '0') AS paid
  FROM parts WHERE length > '0'
  GROUP BY employee_id, week_of
)
SELECT t.employee_id, concat_ws(' ', e.first_name, e.middle_name, e.last_name) AS employee_name, t.week_of,
       trunc(extract(epoch FROM t.worked) / 3600) || ':' || to_char(trunc(extract(epoch FROM t.worked) / 60) % 60, 'FM00') AS hours,
       trunc(extract(epoch FROM t.paid) / 3600) || ':' || to_char(trunc(extract(epoch FROM t.paid) / 60) % 60, 'FM00') AS breaks
FROM totals t
JOIN employees e ON e.id = t.employee_id
ORDER BY t.week_of DESC, employee_name;

DROP FUNCTION IF EXISTS punch_intervals();
