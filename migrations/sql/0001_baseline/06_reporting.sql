-- reporting: export jobs, the export policy and the reporting projection.
-- Tables with their keys and checks, then their indexes. Foreign keys are in 07_foreign_keys.sql.

CREATE TABLE reporting.audit_events (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    organization_id uuid,
    actor_id uuid,
    action text NOT NULL,
    resource_id uuid NOT NULL,
    details jsonb DEFAULT '{}' NOT NULL,
    created_at timestamptz DEFAULT clock_timestamp() NOT NULL,
    trace_id uuid DEFAULT (NULLIF(current_setting('nexus.request_id'::text, true), ''::text))::uuid,
    CONSTRAINT audit_events_pkey PRIMARY KEY (id)
);
CREATE INDEX audit_trace_idx ON reporting.audit_events (trace_id) WHERE (trace_id IS NOT NULL);

CREATE TABLE reporting.export_jobs (
    id uuid NOT NULL,
    organization_id uuid NOT NULL,
    actor_id uuid NOT NULL,
    kind text NOT NULL,
    filters jsonb NOT NULL,
    state text DEFAULT 'queued' NOT NULL,
    created_at timestamptz DEFAULT clock_timestamp() NOT NULL,
    available_at timestamptz DEFAULT clock_timestamp() NOT NULL,
    expires_at timestamptz NOT NULL,
    lease_until timestamptz,
    fence bigint DEFAULT 0 NOT NULL,
    attempts integer DEFAULT 0 NOT NULL,
    error_code text,
    row_count integer,
    completed_at timestamptz,
    object_key text,
    size_bytes bigint,
    purge_at timestamptz,
    CONSTRAINT export_jobs_kind_check CHECK ((kind = ANY (ARRAY['requests'::text, 'charges'::text, 'ledger'::text, 'attempt_costs'::text]))),
    CONSTRAINT export_jobs_object_key_check CHECK ((object_key ~ '^exports/'::text)),
    CONSTRAINT export_jobs_size_bytes_check CHECK ((size_bytes >= 0)),
    CONSTRAINT export_jobs_state_check CHECK ((state = ANY (ARRAY['queued'::text, 'running'::text, 'ready'::text, 'failed'::text, 'expired'::text]))),
    CONSTRAINT export_ready_object CHECK (((state <> 'ready'::text) OR ((object_key IS NOT NULL) AND (size_bytes IS NOT NULL)))),
    CONSTRAINT export_jobs_pkey PRIMARY KEY (id)
);
CREATE INDEX export_due ON reporting.export_jobs (available_at, id) WHERE (state = ANY (ARRAY['queued'::text, 'running'::text]));
CREATE INDEX export_expiry ON reporting.export_jobs (expires_at) WHERE (state <> 'expired'::text);
CREATE INDEX export_org ON reporting.export_jobs (organization_id, created_at, id);
CREATE INDEX export_purge ON reporting.export_jobs (purge_at) WHERE (purge_at IS NOT NULL);

CREATE TABLE reporting.export_policy (
    singleton boolean DEFAULT true NOT NULL,
    per_org_minute integer DEFAULT 10 NOT NULL,
    ttl_seconds integer DEFAULT 86400 NOT NULL,
    max_rows integer DEFAULT 10000 NOT NULL,
    version integer DEFAULT 1 NOT NULL,
    managed_sha256 text,
    managed_at timestamptz,
    CONSTRAINT export_policy_check CHECK (((managed_sha256 IS NULL) = (managed_at IS NULL))),
    CONSTRAINT export_policy_managed_sha256_check CHECK ((managed_sha256 ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT export_policy_max_rows_check CHECK (((max_rows >= 1) AND (max_rows <= 100000))),
    CONSTRAINT export_policy_per_org_minute_check CHECK (((per_org_minute >= 1) AND (per_org_minute <= 1000))),
    CONSTRAINT export_policy_singleton_check CHECK (singleton),
    CONSTRAINT export_policy_ttl_seconds_check CHECK (((ttl_seconds >= 60) AND (ttl_seconds <= 2592000))),
    CONSTRAINT export_policy_version_check CHECK ((version > 0)),
    CONSTRAINT export_policy_pkey PRIMARY KEY (singleton)
);

CREATE TABLE reporting.request_facts (
    id uuid NOT NULL,
    organization_id uuid NOT NULL,
    project_id uuid NOT NULL,
    api_key_id uuid NOT NULL,
    model_id uuid NOT NULL,
    occurred_at timestamptz NOT NULL,
    posted_at timestamptz,
    generation bigint NOT NULL,
    source_at timestamptz NOT NULL,
    copied_at timestamptz DEFAULT clock_timestamp() NOT NULL,
    payload jsonb NOT NULL,
    CONSTRAINT request_facts_pkey PRIMARY KEY (id)
);
CREATE INDEX report_occurrence ON reporting.request_facts (organization_id, occurred_at, id);
CREATE INDEX report_posting ON reporting.request_facts (organization_id, posted_at, id);
