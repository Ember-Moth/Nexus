CREATE TABLE billing.report_changes (
 request_id uuid PRIMARY KEY REFERENCES billing.request_runs,
 generation bigint NOT NULL DEFAULT 1, copied_generation bigint NOT NULL DEFAULT 0,
 changed_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX report_pending ON billing.report_changes(changed_at,request_id)
 WHERE generation>copied_generation;
CREATE FUNCTION billing.mark_report_change() RETURNS trigger LANGUAGE plpgsql
 SET search_path=pg_catalog,pg_temp AS $$
DECLARE rid uuid;
BEGIN
 IF TG_TABLE_NAME='request_runs' THEN rid=NEW.id; ELSE rid=NEW.request_id; END IF;
 INSERT INTO billing.report_changes(request_id) VALUES(rid)
 ON CONFLICT(request_id) DO UPDATE SET generation=billing.report_changes.generation+1,
 changed_at=clock_timestamp();
 RETURN NEW;
END $$;
CREATE TRIGGER request_report AFTER INSERT OR UPDATE ON billing.request_runs
 FOR EACH ROW EXECUTE FUNCTION billing.mark_report_change();
CREATE TRIGGER attempt_report AFTER INSERT OR UPDATE ON billing.request_attempts
 FOR EACH ROW EXECUTE FUNCTION billing.mark_report_change();
CREATE TRIGGER fact_report AFTER INSERT ON billing.usage_events
 FOR EACH ROW EXECUTE FUNCTION billing.mark_report_change();
INSERT INTO billing.report_changes(request_id) SELECT id FROM billing.request_runs;
GRANT SELECT,INSERT,UPDATE ON billing.report_changes TO nexus_billing;

CREATE TABLE reporting.request_facts (
 id uuid PRIMARY KEY, organization_id uuid NOT NULL, project_id uuid NOT NULL,
 api_key_id uuid NOT NULL,model_id uuid NOT NULL, occurred_at timestamptz NOT NULL,
 posted_at timestamptz, generation bigint NOT NULL,source_at timestamptz NOT NULL,
 copied_at timestamptz NOT NULL DEFAULT clock_timestamp(),payload jsonb NOT NULL
);
CREATE INDEX report_occurrence ON reporting.request_facts(organization_id,occurred_at,id);
CREATE INDEX report_posting ON reporting.request_facts(organization_id,posted_at,id);
CREATE TABLE reporting.export_jobs (
 id uuid PRIMARY KEY,organization_id uuid NOT NULL,actor_id uuid NOT NULL,
 kind text NOT NULL CHECK(kind IN ('requests','charges','ledger','attempt_costs')),
 filters jsonb NOT NULL,state text NOT NULL DEFAULT 'queued'
 CHECK(state IN ('queued','running','ready','failed','expired')),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),available_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 expires_at timestamptz NOT NULL,lease_until timestamptz,fence bigint NOT NULL DEFAULT 0,
 attempts integer NOT NULL DEFAULT 0,error_code text,row_count integer,content bytea,completed_at timestamptz
);
CREATE INDEX export_due ON reporting.export_jobs(available_at,id) WHERE state IN ('queued','running');
CREATE INDEX export_org ON reporting.export_jobs(organization_id,created_at,id);
GRANT SELECT,INSERT,UPDATE ON reporting.request_facts,reporting.export_jobs TO nexus_reporting;

CREATE TABLE billing.review_cases (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),organization_id uuid NOT NULL,
 source_type text NOT NULL CHECK(source_type IN ('request','attempt','account')),
 source_id uuid NOT NULL,request_id uuid REFERENCES billing.request_runs,
 reason text NOT NULL,state text NOT NULL DEFAULT 'open' CHECK(state IN ('open','resolved')),
 version integer NOT NULL DEFAULT 1, evidence jsonb NOT NULL DEFAULT '{}',
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),resolved_at timestamptz,
 resolution_id uuid, UNIQUE(source_type,source_id)
);
CREATE TABLE billing.case_decisions (
 id uuid PRIMARY KEY,case_id uuid NOT NULL,organization_id uuid NOT NULL,actor_id uuid NOT NULL,
 action text NOT NULL,reason text NOT NULL,evidence text NOT NULL,parameters jsonb NOT NULL,
 result jsonb NOT NULL,created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
-- One row per account. The Worker claims a due row for a lease, checks the account against a
-- read-only snapshot and records the result; ledger_sequence is the ledger prefix that passed the
-- per-entry checks, so the next check starts after it.
CREATE TABLE billing.account_checks (
 organization_id uuid PRIMARY KEY,checked_at timestamptz NOT NULL DEFAULT '-infinity',
 differences integer NOT NULL DEFAULT 0,ledger_sequence bigint NOT NULL DEFAULT 0,
 claim uuid,claimed_until timestamptz
);
CREATE INDEX account_checks_due ON billing.account_checks(checked_at);
CREATE FUNCTION billing.track_account_check() RETURNS trigger LANGUAGE plpgsql
 SET search_path=pg_catalog,pg_temp AS $$
BEGIN
 INSERT INTO billing.account_checks(organization_id) VALUES(NEW.organization_id)
 ON CONFLICT(organization_id) DO NOTHING;
 RETURN NEW;
END $$;
CREATE TRIGGER account_check AFTER INSERT ON billing.accounts
 FOR EACH ROW EXECUTE FUNCTION billing.track_account_check();
INSERT INTO billing.account_checks(organization_id) SELECT organization_id FROM billing.accounts;
CREATE TABLE billing.cost_corrections (
 id uuid PRIMARY KEY,attempt_id uuid NOT NULL REFERENCES billing.request_attempts,
 decision_id uuid NOT NULL UNIQUE,amount numeric(28,12) NOT NULL CHECK(amount>=0 AND amount<>'NaN'::numeric),
 quantities jsonb NOT NULL,cost_price_version_id uuid NOT NULL REFERENCES billing.price_versions,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
ALTER TABLE billing.payment_cases ADD COLUMN version integer NOT NULL DEFAULT 1,
 ADD COLUMN decision_id uuid, ADD COLUMN decision_basis jsonb;
GRANT SELECT,INSERT,UPDATE ON billing.review_cases,billing.account_checks TO nexus_billing;
GRANT SELECT,INSERT ON billing.case_decisions,billing.cost_corrections TO nexus_billing;
CREATE INDEX review_queue ON billing.review_cases(organization_id,state,created_at,id);
CREATE FUNCTION billing.track_request_review() RETURNS trigger LANGUAGE plpgsql
 SET search_path=pg_catalog,pg_temp AS $$
BEGIN
 IF NEW.state='review' THEN
  INSERT INTO billing.review_cases(organization_id,source_type,source_id,request_id,reason)
  VALUES(NEW.organization_id,'request',NEW.id,NEW.id,coalesce(NEW.review_reason,'unknown'))
  ON CONFLICT(source_type,source_id) DO UPDATE SET reason=excluded.reason,state='open',
  resolved_at=NULL,resolution_id=NULL,version=billing.review_cases.version+1
  WHERE billing.review_cases.state<>'open' OR billing.review_cases.reason<>excluded.reason;
 ELSIF NEW.reservation_state<>'held' THEN
  UPDATE billing.review_cases SET state='resolved',resolved_at=clock_timestamp(),version=version+1
  WHERE source_type='request' AND source_id=NEW.id AND state='open';
 END IF;
 IF NEW.state='review' OR NEW.reservation_state<>'held' THEN
  INSERT INTO billing.review_cases(organization_id,source_type,source_id,request_id,reason)
  SELECT NEW.organization_id,'attempt',a.id,NEW.id,'missing_attempt_cost'
  FROM billing.request_attempts a WHERE a.request_id=NEW.id AND a.state<>'confirmed'
  AND NOT EXISTS(SELECT FROM billing.cost_corrections c WHERE c.attempt_id=a.id)
  ON CONFLICT(source_type,source_id) DO NOTHING;
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER request_review AFTER INSERT OR UPDATE OF state,review_reason,reservation_state ON billing.request_runs
 FOR EACH ROW EXECUTE FUNCTION billing.track_request_review();
INSERT INTO billing.review_cases(organization_id,source_type,source_id,request_id,reason)
 SELECT organization_id,'request',id,id,coalesce(review_reason,'unknown') FROM billing.request_runs WHERE state='review';
INSERT INTO billing.review_cases(organization_id,source_type,source_id,request_id,reason)
 SELECT r.organization_id,'attempt',a.id,r.id,'missing_attempt_cost' FROM billing.request_attempts a
 JOIN billing.request_runs r ON r.id=a.request_id WHERE a.state<>'confirmed'
 AND (r.reservation_state<>'held' OR r.state='review' OR EXISTS
 (SELECT FROM billing.normalized_usage n WHERE n.attempt_id=a.id AND n.review_reason IS NOT NULL));

CREATE TABLE publication.audit_events (
 id uuid PRIMARY KEY DEFAULT gen_random_uuid(),organization_id uuid,actor_id uuid,
 action text NOT NULL,resource_id uuid NOT NULL,details jsonb NOT NULL DEFAULT '{}',
 created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
GRANT SELECT,INSERT ON publication.audit_events TO nexus_publication;
CREATE TABLE reporting.audit_events (LIKE publication.audit_events INCLUDING ALL);
GRANT SELECT,INSERT ON reporting.audit_events TO nexus_reporting;
ALTER TABLE catalog.audit_events ADD COLUMN organization_id uuid;
UPDATE catalog.audit_events SET organization_id=(details->>'organization_id')::uuid
 WHERE action='route.configured' AND details->>'organization_id' ~ '^[a-fA-F0-9-]{36}$';
UPDATE catalog.audit_events a SET organization_id=q.organization_id FROM catalog.quota_policies q
 WHERE a.action='quota.configured' AND a.resource_id=q.id;
CREATE TABLE billing.account_restrictions(
 account_id uuid NOT NULL REFERENCES billing.accounts, source_type text NOT NULL,
 source_id uuid NOT NULL,created_at timestamptz NOT NULL DEFAULT clock_timestamp(),released_at timestamptz,
 PRIMARY KEY(account_id,source_type,source_id)
);
GRANT SELECT,INSERT,UPDATE ON billing.account_restrictions TO nexus_billing;
INSERT INTO billing.account_restrictions(account_id,source_type,source_id)
 SELECT r.account_id,'request',r.id FROM billing.request_runs r JOIN billing.accounts a ON a.id=r.account_id
 WHERE a.status='frozen' AND r.review_reason='authorized_bound_exceeded';
INSERT INTO billing.account_restrictions(account_id,source_type,source_id)
 SELECT DISTINCT o.account_id,'payment',o.id FROM billing.payment_orders o JOIN billing.accounts a ON a.id=o.account_id
 JOIN billing.payment_cases c ON c.order_id=o.id WHERE a.status='frozen' AND c.status='open';
INSERT INTO billing.account_restrictions(account_id,source_type,source_id)
 SELECT id,'legacy',id FROM billing.accounts a WHERE a.status='frozen' AND NOT EXISTS
 (SELECT FROM billing.account_restrictions f WHERE f.account_id=a.id);
ALTER TABLE billing.request_runs ADD COLUMN review_decision_id uuid REFERENCES billing.case_decisions(id);
CREATE FUNCTION billing.dirty_corrected_cost() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$
BEGIN
 UPDATE billing.report_changes SET generation=generation+1,changed_at=clock_timestamp()
 WHERE request_id=(SELECT request_id FROM billing.request_attempts WHERE id=NEW.attempt_id);
 RETURN NEW;
END $$;
CREATE TRIGGER corrected_cost_report AFTER INSERT ON billing.cost_corrections
 FOR EACH ROW EXECUTE FUNCTION billing.dirty_corrected_cost();
CREATE FUNCTION billing.close_cost_review() RETURNS trigger LANGUAGE plpgsql
 SET search_path=pg_catalog,pg_temp AS $$
BEGIN
 UPDATE billing.review_cases rc SET state='resolved',version=version+1,resolved_at=clock_timestamp()
 WHERE source_type='attempt' AND source_id=NEW.id AND state='open' AND
 (reason<>'statement_difference' OR NEW.cost_amount=(SELECT amount FROM billing.upstream_statements
 WHERE id=(rc.evidence->>'statement_id')::uuid));
 RETURN NEW;
END $$;
CREATE TRIGGER resolved_attempt_review AFTER UPDATE OF state ON billing.request_attempts
 FOR EACH ROW WHEN (NEW.state='confirmed') EXECUTE FUNCTION billing.close_cost_review();
ALTER TABLE billing.cost_corrections ADD CONSTRAINT cost_decision_reference
 FOREIGN KEY(decision_id) REFERENCES billing.case_decisions DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE billing.ingest_counters(name text PRIMARY KEY,total bigint NOT NULL DEFAULT 0);
GRANT SELECT,INSERT,UPDATE ON billing.ingest_counters TO nexus_billing;
CREATE TABLE billing.upstream_statements (
 id uuid PRIMARY KEY,organization_id uuid NOT NULL,attempt_id uuid NOT NULL REFERENCES billing.request_attempts,
 external_reference text NOT NULL,currency text NOT NULL,amount numeric(28,12) NOT NULL CHECK(amount>=0 AND amount<>'NaN'::numeric),
 compared_cost numeric(28,12),evidence text NOT NULL,actor_id uuid NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),UNIQUE(attempt_id,external_reference)
);
GRANT SELECT,INSERT ON billing.upstream_statements TO nexus_billing;

CREATE FUNCTION billing.bump_review_evidence() RETURNS trigger LANGUAGE plpgsql
 SET search_path=pg_catalog,pg_temp AS $$ BEGIN
 UPDATE billing.review_cases SET version=version+1 WHERE request_id=NEW.request_id AND state='open';
 RETURN NEW;
END $$;
CREATE TRIGGER new_review_evidence AFTER INSERT ON billing.usage_events
 FOR EACH ROW EXECUTE FUNCTION billing.bump_review_evidence();
ALTER TABLE billing.payment_observations ADD COLUMN query_elapsed_ms integer;
CREATE INDEX export_expiry ON reporting.export_jobs(expires_at) WHERE state<>'expired';

ALTER TABLE billing.payment_orders ADD COLUMN verified_observation_id uuid;
ALTER TABLE billing.payment_orders ADD FOREIGN KEY(id,verified_observation_id)
 REFERENCES billing.payment_observations(order_id,id);
