-- daily_hours: weekly_hours by local calendar day, one row per employee per day with time in it (owner, 2026-11-04).
-- Both views now read hour_totals(period), which holds weekly_hours's query with the week made a parameter: a shift,
-- break or lunch across midnight splits there as one across Monday 00:00 does. Each row's minutes are truncated on their
-- own, so a week's days can add up to a few minutes less than its weekly_hours row. No grants: only the owner reads it.

-- Each employee's totals per period ('day' or 'week', a date_trunc field), in the backend's zone, h:mm with seconds dropped.
CREATE FUNCTION hour_totals(period text)
RETURNS TABLE (employee_id bigint, period_start date, hours text, breaks text, lunches text, unpaid_breaks text)
LANGUAGE sql STABLE AS $$
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
-- breaks. A period edge then clips the head and the break alike, never changing how much of the break is paid.
spans AS (
  SELECT i.employee_id, i.kind, i.starts, i.ends FROM i
  UNION ALL
  SELECT i.employee_id, 'paid', i.starts,
         i.starts + greatest(least(i.ends - i.starts, env.allowance - coalesce(sum(i.ends - i.starts) OVER (
           PARTITION BY i.employee_id, (i.starts AT TIME ZONE env.tz)::date
           ORDER BY i.starts ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING), '0')), '0')
  FROM i CROSS JOIN env WHERE i.kind = 'break'
),
-- Each span's part in each period it touches; a span ending at a period's 00:00 touches it with nothing, left out.
parts AS (
  SELECT s.employee_id, s.kind, p.start::date AS period_start,
         least(s.ends, (p.start + ('1 ' || period)::interval) AT TIME ZONE env.tz) - greatest(s.starts, p.start AT TIME ZONE env.tz)
           AS length
  FROM spans s CROSS JOIN env
  CROSS JOIN generate_series(date_trunc(period, s.starts AT TIME ZONE env.tz), date_trunc(period, s.ends AT TIME ZONE env.tz),
                             ('1 ' || period)::interval) p (start)
),
-- Whole minutes, seconds dropped. Unpaid break time is a break's part past its paid head, both clipped to the period.
totals AS (
  SELECT employee_id, period_start,
         trunc(extract(epoch FROM sum(CASE WHEN kind = 'shift' THEN length WHEN kind <> 'paid' THEN -length END)) / 60) AS worked,
         trunc(extract(epoch FROM coalesce(sum(length) FILTER (WHERE kind = 'paid'), '0')) / 60) AS paid,
         trunc(extract(epoch FROM coalesce(sum(length) FILTER (WHERE kind = 'lunch'), '0')) / 60) AS lunch,
         trunc(extract(epoch FROM coalesce(sum(CASE kind WHEN 'break' THEN length WHEN 'paid' THEN -length END), '0')) / 60) AS unpaid
  FROM parts WHERE length > '0'
  GROUP BY employee_id, period_start
)
SELECT t.employee_id, t.period_start,
       trunc(t.worked / 60) || ':' || to_char(t.worked % 60, 'FM00'),
       trunc(t.paid / 60) || ':' || to_char(t.paid % 60, 'FM00'),
       trunc(t.lunch / 60) || ':' || to_char(t.lunch % 60, 'FM00'),
       trunc(t.unpaid / 60) || ':' || to_char(t.unpaid % 60, 'FM00')
FROM totals t
$$;
REVOKE EXECUTE ON FUNCTION hour_totals(text) FROM PUBLIC;

CREATE OR REPLACE VIEW weekly_hours AS
SELECT t.employee_id, concat_ws(' ', e.first_name, e.middle_name, e.last_name) AS employee_name, t.period_start AS week_of,
       t.hours, t.breaks, t.lunches, t.unpaid_breaks
FROM hour_totals('week') t
JOIN employees e ON e.id = t.employee_id
ORDER BY t.period_start DESC, employee_name;

CREATE VIEW daily_hours AS
SELECT t.employee_id, concat_ws(' ', e.first_name, e.middle_name, e.last_name) AS employee_name, t.period_start AS day,
       t.hours, t.breaks, t.lunches, t.unpaid_breaks
FROM hour_totals('day') t
JOIN employees e ON e.id = t.employee_id
ORDER BY t.period_start DESC, employee_name;
