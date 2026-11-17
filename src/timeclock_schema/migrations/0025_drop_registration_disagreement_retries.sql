-- timeclock-schema 0025: a registration never gives up (owner, 2026-11-17): it ends on a passing set, the press timeout
-- or Cancel, so the cap on disagreeing presses is gone. Every clock reads settings by named columns and stopped reading
-- this one in timeclock-system 0.14.0, before this ran. Its constraints and column privileges go with it.

ALTER TABLE config.settings DROP COLUMN registration_disagreement_retries;
