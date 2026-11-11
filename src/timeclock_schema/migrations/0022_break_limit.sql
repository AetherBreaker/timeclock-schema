-- timeclock-schema 0022: a per-break limit on paid break time (owner, 2026-11-11). Each break is paid up to
-- config.settings.break_limit_minutes exactly, seconds counted; 0 turns the limit off. Only a break's paid part counts
-- against the day's paid_break_minutes, so at 15 and 30 two long breaks pay 15 each and a third that day nothing.
-- shift_summary's over-limit columns read the same setting in place of 0010's fixed 15, still by whole minutes (a 15:59
-- break isn't flagged, though its 59 seconds are unpaid), and flag nothing at 0. timeclock_schema.shifts.total_hours holds
-- the same rule; tests/test_sql.py holds the two to the same totals. Otherwise both bodies are 0009's and 0010's.

ALTER TABLE config.settings ADD COLUMN break_limit_minutes integer NOT NULL DEFAULT 15 CHECK (break_limit_minutes >= 0);

CREATE OR REPLACE FUNCTION hour_totals(period text)
RETURNS TABLE (employee_id bigint, period_start date, hours text, breaks text, lunches text, unpaid_breaks text)
LANGUAGE sql STABLE AS $$
-- Lookups, not a join: the two one-row tables have no statistics, so a join of them is estimated at 1.5 million rows and
-- the planner JIT-compiles the query (300 ms, against 8 without). `cap` is NULL with the limit off, and least() skips a
-- NULL argument, so an uncapped break's paid head is the allowance's alone.
WITH env AS (
  SELECT (SELECT tz FROM backend.environment) AS tz,
         (SELECT paid_break_minutes FROM config.settings) * interval '1 minute' AS allowance,
         nullif((SELECT break_limit_minutes FROM config.settings), 0) * interval '1 minute' AS cap
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
-- The intervals, plus each break's paid head: its length up to the cap and to what its local day's allowance still
-- covers. The day's earlier breaks are charged their capped lengths, which equal their paid heads until the allowance
-- runs out, and after that it covers nothing either way. A period edge then clips the head and the break alike, never
-- changing how much of the break is paid.
spans AS (
  SELECT i.employee_id, i.kind, i.starts, i.ends FROM i
  UNION ALL
  SELECT i.employee_id, 'paid', i.starts,
         i.starts + greatest(least(i.ends - i.starts, env.cap, env.allowance - coalesce(sum(least(i.ends - i.starts, env.cap)) OVER (
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

CREATE OR REPLACE VIEW shift_summary AS
-- Lookups, not a join: see hour_totals, as is `cap`. `lim` is the limit in minutes for the over-limit columns; NULL
-- with it off, which no comparison passes.
WITH env AS (
  SELECT (SELECT tz FROM backend.environment) AS tz,
         (SELECT paid_break_minutes FROM config.settings) * interval '1 minute' AS allowance,
         nullif((SELECT break_limit_minutes FROM config.settings), 0) AS lim,
         nullif((SELECT break_limit_minutes FROM config.settings), 0) * interval '1 minute' AS cap
),
-- The punches counted so far, each with its shift's number (clock-ins so far) and the next punch's time.
p AS (
  SELECT c.*, count(*) FILTER (WHERE c.kind = 'clock_in') OVER w AS shift_no, lead(c.at) OVER w AS next_at
  FROM counted_punches() c
  WHERE c.at <= now()
  WINDOW w AS (PARTITION BY c.employee_id ORDER BY c.at, c.id)
),
-- Each break and lunch, to the next punch (its end, or a clock-out) or, still open, to now(); a break's paid part (as
-- hour_totals's paid head), its whole minutes past the limit (NULL when not over) and its times.
iv AS (
  SELECT p.employee_id, p.shift_no, p.kind, p.at AS starts, coalesce(p.next_at, now()) AS ends, p.next_at IS NULL AS open,
         CASE WHEN p.kind = 'break_start' AND trunc(extract(epoch FROM coalesce(p.next_at, now()) - p.at) / 60) > env.lim
           THEN trunc(extract(epoch FROM coalesce(p.next_at, now()) - p.at) / 60) - env.lim END AS over,
         CASE WHEN p.kind = 'break_start' THEN greatest(least(coalesce(p.next_at, now()) - p.at, env.cap, env.allowance - coalesce(
           sum(least(coalesce(p.next_at, now()) - p.at, env.cap)) FILTER (WHERE p.kind = 'break_start') OVER (
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
         count(iv.over) AS breaks_over_limit,
         coalesce(sum(iv.over), 0) AS overage,
         string_agg(iv.times || coalesce(' (+' || iv.over || ')', ''), ', ' ORDER BY iv.starts)
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
