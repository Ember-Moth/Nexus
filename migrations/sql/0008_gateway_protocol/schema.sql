CREATE TABLE publication.gateway_clusters (
 id uuid PRIMARY KEY, name text NOT NULL UNIQUE, region text NOT NULL,
 organization_ids uuid[] NOT NULL, deployment_ids uuid[] NOT NULL,
 public_key bytea NOT NULL CHECK(octet_length(public_key)=32), key_version integer NOT NULL DEFAULT 1,
 active boolean NOT NULL DEFAULT true, desired_revision bigint NOT NULL DEFAULT 0,
 distributed_revision bigint NOT NULL DEFAULT 0, version integer NOT NULL DEFAULT 1,
 CHECK(cardinality(organization_ids)>0 AND cardinality(deployment_ids)>0)
);
CREATE TABLE publication.gateway_principals (
 node_id uuid PRIMARY KEY, cluster_id uuid NOT NULL REFERENCES publication.gateway_clusters,
 database_role name NOT NULL UNIQUE, capabilities text[] NOT NULL, active boolean NOT NULL DEFAULT true,
 applied_revision bigint NOT NULL DEFAULT 0, applied_at timestamptz
);
CREATE TABLE publication.config_snapshots (
 cluster_id uuid NOT NULL REFERENCES publication.gateway_clusters, revision bigint NOT NULL CHECK(revision>0),
 manifest jsonb NOT NULL, payload_bytes bytea NOT NULL, sha256 text NOT NULL,
 source_revision jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(cluster_id, revision),
 CHECK((manifest->>'cluster_id') IS NOT DISTINCT FROM cluster_id::text AND (manifest->>'revision') IS NOT DISTINCT FROM revision::text),
 CHECK(sha256 = encode(sha256(payload_bytes),'hex') AND manifest=convert_from(payload_bytes,'UTF8')::jsonb)
);
CREATE TABLE publication.config_outbox (
 cluster_id uuid NOT NULL, revision bigint NOT NULL, attempts integer NOT NULL DEFAULT 0,
 next_attempt_at timestamptz NOT NULL DEFAULT clock_timestamp(), delivered_at timestamptz,
 PRIMARY KEY(cluster_id,revision), FOREIGN KEY(cluster_id,revision) REFERENCES publication.config_snapshots
);
CREATE TABLE publication.credential_envelopes (
 cluster_id uuid NOT NULL, revision bigint NOT NULL, credential_id uuid NOT NULL,
 credential_version integer NOT NULL, key_version integer NOT NULL, ciphertext bytea NOT NULL,
 PRIMARY KEY(cluster_id,revision,credential_id,credential_version),
 FOREIGN KEY(cluster_id,revision) REFERENCES publication.config_snapshots
);
CREATE TABLE publication.node_config_acks (
 node_id uuid NOT NULL REFERENCES publication.gateway_principals, cluster_id uuid NOT NULL, revision bigint NOT NULL,
 digest text NOT NULL, applied boolean NOT NULL, error_code text, created_at timestamptz DEFAULT clock_timestamp(),
 FOREIGN KEY(cluster_id,revision) REFERENCES publication.config_snapshots
);
CREATE TABLE publication.release_commands (
 cluster_id uuid NOT NULL, actor_id uuid NOT NULL, key_digest text NOT NULL, parameters text NOT NULL,
 revision bigint NOT NULL, PRIMARY KEY(cluster_id,actor_id,key_digest),
 FOREIGN KEY(cluster_id,revision) REFERENCES publication.config_snapshots
);
CREATE TABLE catalog.route_policies (
 organization_id uuid NOT NULL REFERENCES identity.organizations, model_id uuid NOT NULL REFERENCES catalog.models,
 candidates jsonb NOT NULL, version integer NOT NULL DEFAULT 1, PRIMARY KEY(organization_id,model_id)
);
CREATE TABLE catalog.quota_policies (
 id uuid PRIMARY KEY, organization_id uuid NOT NULL REFERENCES identity.organizations,
 layer text NOT NULL CHECK(layer IN ('platform','customer')), scope_type text NOT NULL CHECK(scope_type IN ('organization','project','key')),
 scope_id uuid NOT NULL, metric text NOT NULL CHECK(metric IN ('rpm','tpm','concurrency')),
 limit_value integer NOT NULL CHECK(limit_value>0), version integer NOT NULL DEFAULT 1,
 UNIQUE(organization_id,layer,scope_type,scope_id,metric)
);
ALTER TABLE billing.request_runs ADD COLUMN node_id uuid REFERENCES publication.gateway_principals,
 ADD COLUMN cluster_id uuid, ADD COLUMN config_revision bigint,
 ADD CONSTRAINT gateway_request_snapshot FOREIGN KEY(cluster_id,config_revision) REFERENCES publication.config_snapshots,
 ADD CONSTRAINT gateway_request_origin CHECK ((node_id IS NULL)=(config_revision IS NULL) AND (cluster_id IS NULL)=(node_id IS NULL));
ALTER TABLE billing.request_attempts ADD COLUMN sequence integer CHECK(sequence>0), ADD UNIQUE(request_id,sequence);
CREATE TABLE billing.usage_events (
 id uuid PRIMARY KEY, request_id uuid NOT NULL REFERENCES billing.request_runs, attempt_id uuid REFERENCES billing.request_attempts,
 kind text NOT NULL CHECK(kind IN ('attempt.finished','request.closed')), envelope jsonb NOT NULL,
 received_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE UNIQUE INDEX usage_attempt_finished ON billing.usage_events(attempt_id) WHERE kind='attempt.finished';
CREATE UNIQUE INDEX usage_request_closed ON billing.usage_events(request_id) WHERE kind='request.closed';
CREATE INDEX usage_by_request ON billing.usage_events(request_id);
CREATE TABLE billing.normalized_usage (
 event_id uuid PRIMARY KEY REFERENCES billing.usage_events, attempt_id uuid NOT NULL REFERENCES billing.request_attempts,
 rule_version text NOT NULL, quantities jsonb, review_reason text
);
CREATE TABLE billing.usage_jobs (
 request_id uuid PRIMARY KEY REFERENCES billing.request_runs, state text NOT NULL CHECK(state IN ('ready','waiting','review','done')),
 next_attempt_at timestamptz NOT NULL DEFAULT clock_timestamp(), failures integer NOT NULL DEFAULT 0, generation bigint NOT NULL DEFAULT 1, reason text
);
CREATE INDEX usage_jobs_due ON billing.usage_jobs(next_attempt_at,request_id) WHERE state='ready';
GRANT SELECT,INSERT,UPDATE ON publication.gateway_clusters,publication.gateway_principals,publication.config_outbox TO nexus_publication;
GRANT SELECT,INSERT ON publication.config_snapshots,publication.credential_envelopes,publication.node_config_acks,publication.release_commands TO nexus_publication;
GRANT SELECT,INSERT,UPDATE ON catalog.route_policies,catalog.quota_policies TO nexus_catalog;
GRANT SELECT,INSERT ON billing.usage_events,billing.normalized_usage TO nexus_billing;
GRANT SELECT,INSERT,UPDATE ON billing.usage_jobs TO nexus_billing;
ALTER TABLE publication.gateway_clusters ADD COLUMN scope_generation integer NOT NULL DEFAULT 1;
CREATE TABLE publication.previous_keys (
 cluster_id uuid NOT NULL REFERENCES publication.gateway_clusters,key_version integer NOT NULL,
 valid_until timestamptz NOT NULL,PRIMARY KEY(cluster_id,key_version)
);
GRANT SELECT,INSERT ON publication.previous_keys TO nexus_publication;
ALTER TABLE publication.gateway_principals ADD UNIQUE(cluster_id,node_id);
ALTER TABLE billing.request_runs ADD FOREIGN KEY(cluster_id,node_id) REFERENCES publication.gateway_principals(cluster_id,node_id);
CREATE FUNCTION billing.protect_gateway_binding() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$
BEGIN
 IF ROW(OLD.cluster_id,OLD.node_id,OLD.config_revision) IS DISTINCT FROM ROW(NEW.cluster_id,NEW.node_id,NEW.config_revision)
 THEN RAISE EXCEPTION 'immutable gateway binding'; END IF; RETURN NEW;
END $$;
CREATE TRIGGER gateway_request_origin BEFORE UPDATE OF cluster_id,node_id,config_revision ON billing.request_runs
FOR EACH ROW EXECUTE FUNCTION billing.protect_gateway_binding();
CREATE TRIGGER immutable_usage_fact BEFORE UPDATE OR DELETE ON billing.usage_events
FOR EACH ROW EXECUTE FUNCTION billing.reject_immutable_change();

CREATE TABLE publication.limit_namespaces(organization_id uuid PRIMARY KEY);
GRANT SELECT, INSERT ON publication.limit_namespaces TO nexus_publication;
