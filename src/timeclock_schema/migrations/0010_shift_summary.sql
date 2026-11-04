-- shift_summary: one row per shift, a counted clock-in and its clock-out, with the breaks and lunches between (owner,
-- 2026-11-04). Durations are h:mm, seconds dropped, by weekly_hours's rules: a shift still open (or ended by a clock-out
-- stamped after now()) runs to now(), and paid break time is the local day's allowance, charged earliest first across
-- the day's shifts. A shift is one row even across midnight, under its clock-in's day. A break is over the 15-minute
-- limit by its whole minutes past 15 (a 15:59 break isn't); the limit is fixed here (owner). corrected: a punch of the
-- shift was amended or added by hand, by the owner's postgres login (which the backend's automatic clock-out shares).
-- Times read 24-hour, in the backend's zone. No grants: only the owner reads it.

-- Whole minutes as h:mm, hours not padded.
CREATE FUNCTION h_mm(minutes numeric) RETURNS text LANGUAGE sql IMMUTABLE AS $$
  SELECT trunc(minutes / 60) || ':' || to_char(minutes % 60, 'FM00')
$$;

CREATE VIEW shift_summary AS
-- Lookups, not a join: see hour_totals.
WITH env AS (
  SELECT (SELECT tz FROM backend.environment) AS tz,
         (SELECT paid_break_minutes FROM config.settings) * interval '1 minute' AS allowance
),
-- The punches counted so far, each with its shift's number (clock-ins so far) and the next punch's time.
p AS (
  SELECT c.*, count(*) FILTER (WHERE c.kind = 'clock_in') OVER w AS shift_no, lead(c.at) OVER w AS next_at
  FROM counted_punches() c
  WHERE c.at <= now()
  WINDOW w AS (PARTITION BY c.employee_id ORDER BY c.at, c.id)
),
-- Each break and lunch, to the next punch (its end, or a clock-out) or, still open, to now(); a break's paid part and
-- its whole minutes.
iv AS (
  SELECT p.employee_id, p.shift_no, p.kind, p.at AS starts, coalesce(p.next_at, now()) AS ends, p.next_at IS NULL AS open,
         trunc(extract(epoch FROM coalesce(p.next_at, now()) - p.at) / 60) AS minutes,
         CASE WHEN p.kind = 'break_start' THEN greatest(least(coalesce(p.next_at, now()) - p.at, env.allowance - coalesce(
           sum(coalesce(p.next_at, now()) - p.at) FILTER (WHERE p.kind = 'break_start') OVER (
             PARTITION BY p.employee_id, (p.at AT TIME ZONE env.tz)::date
             ORDER BY p.at ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING), '0')), '0') END AS paid,
         to_char(p.at AT TIME ZONE env.tz, 'HH24:MI') || '-'
           || CASE WHEN p.next_at IS NULL THEN '' ELSE to_char(p.next_at AT TIME ZONE env.tz, 'HH24:MI') END AS times
  FROM p CROSS JOIN env
  WHERE p.kind IN ('break_start', 'lunch_start')
),
gaps AS (
  SELECT iv.employee_id, iv.shift_no,
         coalesce(sum(iv.ends - iv.starts) FILTER (WHERE iv.kind = 'break_start'), '0') AS break_time,
         coalesce(sum(iv.paid), '0') AS paid,
         coalesce(sum(iv.ends - iv.starts) FILTER (WHERE iv.kind = 'lunch_start'), '0') AS lunch_time,
         count(*) FILTER (WHERE iv.kind = 'break_start') AS break_count,
         count(*) FILTER (WHERE iv.kind = 'lunch_start') AS lunch_count,
         count(*) FILTER (WHERE iv.kind = 'break_start' AND iv.minutes > 15) AS breaks_over_limit,
         coalesce(sum(iv.minutes - 15) FILTER (WHERE iv.kind = 'break_start' AND iv.minutes > 15), 0) AS overage,
         string_agg(iv.times || CASE WHEN iv.minutes > 15 THEN ' (+' || (iv.minutes - 15) || ')' ELSE '' END, ', ' ORDER BY iv.starts)
           FILTER (WHERE iv.kind = 'break_start') AS break_times,
         string_agg(iv.times, ', ' ORDER BY iv.starts) FILTER (WHERE iv.kind = 'lunch_start') AS lunch_times
  FROM iv
  GROUP BY iv.employee_id, iv.shift_no
),
shifts AS (
  SELECT p.employee_id, p.shift_no,
         min(p.at) FILTER (WHERE p.kind = 'clock_in') AS clock_in,
         min(p.at) FILTER (WHERE p.kind = 'clock_out') AS clock_out,
         min(p.instance_id) FILTER (WHERE p.kind = 'clock_in') AS clock,
         coalesce(bool_or(p.automatic) FILTER (WHERE p.kind = 'clock_out'), false) AS automatic_clock_out,
         bool_or(p.corrected_at IS NOT NULL OR (p.db_login = 'postgres' AND NOT p.automatic)) AS corrected
  FROM p
  GROUP BY p.employee_id, p.shift_no
)
SELECT s.employee_id, concat_ws(' ', e.first_name, e.middle_name, e.last_name) AS employee_name,
       (s.clock_in AT TIME ZONE env.tz)::date AS day, date_trunc('week', s.clock_in AT TIME ZONE env.tz)::date AS week_of,
       s.clock, s.clock_in, s.clock_out, s.automatic_clock_out, s.corrected,
       h_mm(m.shift) AS shift_length,
       h_mm(trunc(extract(epoch FROM coalesce(s.clock_out, now()) - s.clock_in - coalesce(g.break_time, '0') - coalesce(g.lunch_time, '0')) / 60))
         AS hours,
       h_mm(trunc(extract(epoch FROM coalesce(g.paid, '0')) / 60)) AS breaks,
       h_mm(trunc(extract(epoch FROM coalesce(g.break_time - g.paid, '0')) / 60)) AS unpaid_breaks,
       h_mm(trunc(extract(epoch FROM coalesce(g.lunch_time, '0')) / 60)) AS lunches,
       coalesce(g.break_count, 0) AS break_count, coalesce(g.lunch_count, 0) AS lunch_count,
       coalesce(g.breaks_over_limit, 0) AS breaks_over_limit, h_mm(coalesce(g.overage, 0)) AS break_overage,
       coalesce(g.break_times, '') AS break_times, coalesce(g.lunch_times, '') AS lunch_times
FROM shifts s
CROSS JOIN env
CROSS JOIN LATERAL (SELECT trunc(extract(epoch FROM coalesce(s.clock_out, now()) - s.clock_in) / 60) AS shift) m
LEFT JOIN gaps g ON g.employee_id = s.employee_id AND g.shift_no = s.shift_no
JOIN employees e ON e.id = s.employee_id
ORDER BY s.clock_in DESC, employee_name;
