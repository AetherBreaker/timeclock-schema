-- weekly_hours, for the weekly payroll export (owner, 2026-11-04): one row per employee per week with time in it, the
-- week being Monday 00:00 to Monday 00:00 in the backend's zone (the clock's week, R33). An export or a backdate filters
-- on `week_of`, the week's Monday. `hours` is shift time less every lunch and break; `breaks` is paid break time only:
-- up to config.settings.paid_break_minutes a local day, charged earliest first. Lunch and break time over that is unpaid
-- and in neither. Both read h:mm, seconds dropped. The counting rules are timeclock_schema.shifts.total_hours's, repeated
-- here; tests/test_sql.py holds the two to the same totals. Applied by hand to both databases first, hence OR REPLACE.
-- No grants: only the owner reads it.

-- Each employee's shifts, breaks and lunches, walking their punches as corrected (punch_log) through the state machine
-- as build_timeline does: a punch not valid from the current state is ignored, and a clock-out ends an open break or
-- lunch. A punch stamped after now() doesn't count yet (the automatic clock-out may be written early); whatever is still
-- open ends at now(). A loop, not a recursive query: that one's cost grows with the square of an employee's punches.
CREATE OR REPLACE FUNCTION punch_intervals()
RETURNS TABLE (employee_id bigint, kind text, starts timestamptz, ends timestamptz) LANGUAGE plpgsql STABLE AS $$
DECLARE
  emp      bigint;
  p        record;
  state    text;  -- off, working, break or lunch
  shift_in timestamptz;
  opened   timestamptz;  -- the open break's or lunch's start
BEGIN
  FOR emp IN SELECT DISTINCT l.employee_id FROM punch_log l LOOP
    state := 'off';
    FOR p IN SELECT l.kind::text AS kind, l.at FROM punch_log l
             WHERE l.employee_id = emp AND l.at <= now() ORDER BY l.at, l.id LOOP
      CONTINUE WHEN NOT (state = 'off' AND p.kind = 'clock_in'
        OR state = 'working' AND p.kind IN ('clock_out', 'break_start', 'lunch_start')
        OR state IN ('break', 'lunch') AND p.kind IN (state || '_end', 'clock_out'));
      IF state IN ('break', 'lunch') THEN
        RETURN QUERY VALUES (emp, state, opened, p.at);
      END IF;
      IF p.kind = 'clock_in' THEN
        shift_in := p.at;
        state := 'working';
      ELSIF p.kind = 'clock_out' THEN
        RETURN QUERY VALUES (emp, 'shift', shift_in, p.at);
        state := 'off';
      ELSIF p.kind IN ('break_start', 'lunch_start') THEN
        opened := p.at;
        state := split_part(p.kind, '_', 1);
      ELSE
        state := 'working';
      END IF;
    END LOOP;
    IF state IN ('break', 'lunch') THEN
      RETURN QUERY VALUES (emp, state, opened, now());
    END IF;
    IF state <> 'off' THEN
      RETURN QUERY VALUES (emp, 'shift', shift_in, now());
    END IF;
  END LOOP;
END $$;
REVOKE EXECUTE ON FUNCTION punch_intervals() FROM PUBLIC;

CREATE OR REPLACE VIEW weekly_hours AS
-- Lookups, not a join: the two one-row tables have no statistics, so a join of them is estimated at 1.5 million rows and
-- the planner JIT-compiles the query (300 ms, against 8 without).
WITH env AS (
  SELECT (SELECT tz FROM backend.environment) AS tz,
         (SELECT paid_break_minutes FROM config.settings) * interval '1 minute' AS allowance
),
i AS (
  SELECT * FROM punch_intervals()
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
