-- A container session is billed by the supplier from creation until it is deleted or idles
-- out. Its funds are a chain of immutable holds: the first reservation (the meter's own
-- request) and extension reservations added before an operation could outlast them.
ALTER TABLE billing.container_sessions
 ADD COLUMN covered_minutes integer NOT NULL DEFAULT 0 CHECK(covered_minutes>=0),
 ADD COLUMN potential_end timestamptz,
 ADD COLUMN idle_minutes integer NOT NULL DEFAULT 20 CHECK(idle_minutes BETWEEN 1 AND 1440);
UPDATE billing.container_sessions s SET covered_minutes=CASE WHEN s.tariff='session_20m'
   THEN r.container_minutes_limit/20*20 ELSE r.container_minutes_limit END,
 idle_minutes=greatest(1,least(1440,ceil(extract(epoch FROM s.review_after-s.last_seen)/60)::integer)),
 potential_end=greatest(s.review_after,s.created_at)
FROM billing.request_runs r WHERE r.id=s.id;
UPDATE billing.container_sessions SET potential_end=review_after WHERE potential_end IS NULL;
ALTER TABLE billing.container_sessions ALTER COLUMN potential_end SET NOT NULL;
CREATE TABLE billing.container_extensions(
 request_id uuid PRIMARY KEY REFERENCES billing.request_runs,meter uuid NOT NULL REFERENCES billing.container_sessions,
 origin uuid NOT NULL,sequence integer NOT NULL CHECK(sequence>0),start_minute integer NOT NULL CHECK(start_minute>=0),
 minutes integer NOT NULL CHECK(minutes>0),attempt_id uuid NOT NULL UNIQUE DEFAULT gen_random_uuid(),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),UNIQUE(meter,sequence)
);
CREATE INDEX container_extension_meter ON billing.container_extensions(meter);
