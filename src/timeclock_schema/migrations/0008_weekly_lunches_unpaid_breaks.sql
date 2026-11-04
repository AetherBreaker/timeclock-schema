-- weekly_hours adds each week's lunch time and unpaid break time, the break time beyond the daily allowance (owner,
-- 2026-11-04), h:mm like the rest; with hours and breaks they make up all the shift time. New columns go last: CREATE OR
-- REPLACE can only add at the end. Applied by hand to both databases and recorded in schema_migrations with 0003-0007,
-- which had been applied by hand unrecorded: rerun, 0004 and 0007 would fail to take this view back to fewer columns.
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
-- Whole minutes, seconds dropped. Unpaid break time is a break's part past its paid head, both clipped to the week.
totals AS (
  SELECT employee_id, week_of,
         trunc(extract(epoch FROM sum(CASE WHEN kind = 'shift' THEN length WHEN kind <> 'paid' THEN -length END)) / 60) AS worked,
         trunc(extract(epoch FROM coalesce(sum(length) FILTER (WHERE kind = 'paid'), '0')) / 60) AS paid,
         trunc(extract(epoch FROM coalesce(sum(length) FILTER (WHERE kind = 'lunch'), '0')) / 60) AS lunch,
         trunc(extract(epoch FROM coalesce(sum(CASE kind WHEN 'break' THEN length WHEN 'paid' THEN -length END), '0')) / 60) AS unpaid
  FROM parts WHERE length > '0'
  GROUP BY employee_id, week_of
)
SELECT t.employee_id, concat_ws(' ', e.first_name, e.middle_name, e.last_name) AS employee_name, t.week_of,
       trunc(t.worked / 60) || ':' || to_char(t.worked % 60, 'FM00') AS hours,
       trunc(t.paid / 60) || ':' || to_char(t.paid % 60, 'FM00') AS breaks,
       trunc(t.lunch / 60) || ':' || to_char(t.lunch % 60, 'FM00') AS lunches,
       trunc(t.unpaid / 60) || ':' || to_char(t.unpaid % 60, 'FM00') AS unpaid_breaks
FROM totals t
JOIN employees e ON e.id = t.employee_id
ORDER BY t.week_of DESC, employee_name;
