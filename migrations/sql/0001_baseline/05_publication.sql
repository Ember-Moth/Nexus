-- publication: gateway clusters and nodes, configuration snapshots and their delivery, and the
-- upstream resources (files, conversations, containers, batches, vector stores) the Relay keeps
-- per key.
-- Tables with their keys and checks, then their indexes. Foreign keys are in 07_foreign_keys.sql.

CREATE TABLE publication.audit_events (
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
CREATE INDEX audit_trace_idx ON publication.audit_events (trace_id) WHERE (trace_id IS NOT NULL);

CREATE TABLE publication.batch_items (
    batch_id uuid NOT NULL,
    custom_id varchar(64) NOT NULL,
    rid uuid NOT NULL,
    aid uuid NOT NULL,
    model_id uuid NOT NULL,
    deployment_id uuid NOT NULL,
    CONSTRAINT batch_items_custom_id_check CHECK (((custom_id)::text ~ '^[A-Za-z0-9_-]{1,64}$'::text)),
    CONSTRAINT batch_items_pkey PRIMARY KEY (batch_id, custom_id),
    CONSTRAINT batch_items_aid_key UNIQUE (aid),
    CONSTRAINT batch_items_rid_key UNIQUE (rid)
);

CREATE TABLE publication.cluster_automation (
    cluster_id uuid NOT NULL,
    settled jsonb,
    content_digest text,
    content_revision bigint,
    price_models uuid[],
    observed jsonb,
    changed_at timestamptz,
    pending_since timestamptz,
    retry_after timestamptz,
    failed jsonb,
    checked_at timestamptz,
    lease_until timestamptz,
    published_revision bigint,
    published_at timestamptz,
    published_trigger jsonb,
    failure_code text,
    failure_message text,
    failure_at timestamptz,
    paused_revision bigint,
    paused_at timestamptz,
    CONSTRAINT cluster_automation_pkey PRIMARY KEY (cluster_id)
);

CREATE TABLE publication.cluster_keys (
    cluster_id uuid NOT NULL,
    key_id uuid NOT NULL,
    added_sequence bigint NOT NULL,
    removed_sequence bigint,
    digest text NOT NULL,
    organization_id uuid NOT NULL,
    project_id uuid NOT NULL,
    service_account_id uuid NOT NULL,
    expires_at timestamptz,
    CONSTRAINT cluster_keys_added_sequence_check CHECK ((added_sequence > 0)),
    CONSTRAINT cluster_keys_check CHECK ((removed_sequence > added_sequence)),
    CONSTRAINT cluster_keys_digest_check CHECK ((digest ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT cluster_keys_pkey PRIMARY KEY (cluster_id, key_id, added_sequence)
);
CREATE INDEX cluster_keys_added ON publication.cluster_keys (cluster_id, added_sequence, key_id);
CREATE INDEX cluster_keys_removed ON publication.cluster_keys (cluster_id, removed_sequence, key_id) WHERE (removed_sequence IS NOT NULL);
CREATE UNIQUE INDEX cluster_keys_live ON publication.cluster_keys (cluster_id, key_id) WHERE (removed_sequence IS NULL);
CREATE UNIQUE INDEX cluster_keys_live_digest ON publication.cluster_keys (cluster_id, digest) WHERE (removed_sequence IS NULL);

CREATE TABLE publication.cluster_policies (
    cluster_id uuid NOT NULL,
    max_attempts integer,
    failover_budget_ms integer,
    purge_grace_seconds integer,
    stream_lifetime_seconds integer,
    auto_publish_quiet_seconds double precision,
    auto_publish_max_delay_seconds double precision,
    version integer DEFAULT 1 NOT NULL,
    CONSTRAINT cluster_policies_auto_publish_max_delay_seconds_check CHECK (((auto_publish_max_delay_seconds > (0)::double precision) AND (auto_publish_max_delay_seconds <= (3600)::double precision))),
    CONSTRAINT cluster_policies_auto_publish_quiet_seconds_check CHECK (((auto_publish_quiet_seconds >= (0)::double precision) AND (auto_publish_quiet_seconds <= (300)::double precision))),
    CONSTRAINT cluster_policies_failover_budget_ms_check CHECK (((failover_budget_ms >= 100) AND (failover_budget_ms <= 60000))),
    CONSTRAINT cluster_policies_max_attempts_check CHECK (((max_attempts >= 1) AND (max_attempts <= 4))),
    CONSTRAINT cluster_policies_purge_grace_seconds_check CHECK ((purge_grace_seconds >= 0)),
    CONSTRAINT cluster_policies_stream_lifetime_seconds_check CHECK ((stream_lifetime_seconds >= 1)),
    CONSTRAINT cluster_policies_version_check CHECK ((version > 0)),
    CONSTRAINT cluster_policies_pkey PRIMARY KEY (cluster_id)
);

CREATE TABLE publication.cluster_policy_defaults (
    singleton boolean DEFAULT true NOT NULL,
    max_attempts integer DEFAULT 2 NOT NULL,
    failover_budget_ms integer DEFAULT 10000 NOT NULL,
    purge_grace_seconds integer DEFAULT 604800 NOT NULL,
    stream_lifetime_seconds integer DEFAULT 1800 NOT NULL,
    auto_publish_quiet_seconds double precision DEFAULT 3 NOT NULL,
    auto_publish_max_delay_seconds double precision DEFAULT 30 NOT NULL,
    version integer DEFAULT 1 NOT NULL,
    managed_sha256 text,
    managed_at timestamptz,
    CONSTRAINT cluster_policy_defaults_auto_publish_max_delay_seconds_check CHECK (((auto_publish_max_delay_seconds > (0)::double precision) AND (auto_publish_max_delay_seconds <= (3600)::double precision))),
    CONSTRAINT cluster_policy_defaults_auto_publish_quiet_seconds_check CHECK (((auto_publish_quiet_seconds >= (0)::double precision) AND (auto_publish_quiet_seconds <= (300)::double precision))),
    CONSTRAINT cluster_policy_defaults_check CHECK (((managed_sha256 IS NULL) = (managed_at IS NULL))),
    CONSTRAINT cluster_policy_defaults_check1 CHECK ((auto_publish_max_delay_seconds >= auto_publish_quiet_seconds)),
    CONSTRAINT cluster_policy_defaults_failover_budget_ms_check CHECK (((failover_budget_ms >= 100) AND (failover_budget_ms <= 60000))),
    CONSTRAINT cluster_policy_defaults_managed_sha256_check CHECK ((managed_sha256 ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT cluster_policy_defaults_max_attempts_check CHECK (((max_attempts >= 1) AND (max_attempts <= 4))),
    CONSTRAINT cluster_policy_defaults_purge_grace_seconds_check CHECK ((purge_grace_seconds >= 0)),
    CONSTRAINT cluster_policy_defaults_singleton_check CHECK (singleton),
    CONSTRAINT cluster_policy_defaults_stream_lifetime_seconds_check CHECK ((stream_lifetime_seconds >= 1)),
    CONSTRAINT cluster_policy_defaults_version_check CHECK ((version > 0)),
    CONSTRAINT cluster_policy_defaults_pkey PRIMARY KEY (singleton)
);

CREATE TABLE publication.config_outbox (
    cluster_id uuid NOT NULL,
    revision bigint NOT NULL,
    attempts integer DEFAULT 0 NOT NULL,
    next_attempt_at timestamptz DEFAULT clock_timestamp() NOT NULL,
    delivered_at timestamptz,
    CONSTRAINT config_outbox_pkey PRIMARY KEY (cluster_id, revision)
);

CREATE TABLE publication.config_snapshots (
    cluster_id uuid NOT NULL,
    revision bigint NOT NULL,
    manifest jsonb NOT NULL,
    payload_bytes bytea NOT NULL,
    sha256 text NOT NULL,
    source_revision jsonb NOT NULL,
    created_at timestamptz DEFAULT clock_timestamp() NOT NULL,
    key_sequence bigint DEFAULT 0 NOT NULL,
    required_capabilities text[] DEFAULT '{}' NOT NULL,
    CONSTRAINT config_snapshots_check CHECK (((NOT ((manifest ->> 'cluster_id'::text) IS DISTINCT FROM (cluster_id)::text)) AND (NOT ((manifest ->> 'revision'::text) IS DISTINCT FROM (revision)::text)))),
    CONSTRAINT config_snapshots_check1 CHECK (((sha256 = encode(sha256(payload_bytes), 'hex'::text)) AND (manifest = (convert_from(payload_bytes, 'UTF8'::name))::jsonb))),
    CONSTRAINT config_snapshots_key_sequence_check CHECK ((key_sequence >= 0)),
    CONSTRAINT config_snapshots_key_set CHECK ((COALESCE((((manifest -> 'key_set'::text) ->> 'sequence'::text))::bigint, (0)::bigint) = key_sequence)),
    CONSTRAINT config_snapshots_revision_check CHECK ((revision > 0)),
    CONSTRAINT config_snapshots_pkey PRIMARY KEY (cluster_id, revision)
);

CREATE TABLE publication.container_files (
    id uuid NOT NULL,
    container_id uuid NOT NULL,
    provider_id text NOT NULL,
    deleted_at timestamptz,
    CONSTRAINT container_files_provider_id_check CHECK ((provider_id ~ '^[A-Za-z0-9_-]{1,200}$'::text)),
    CONSTRAINT container_files_pkey PRIMARY KEY (id),
    CONSTRAINT container_files_container_id_provider_id_key UNIQUE (container_id, provider_id)
);

CREATE TABLE publication.container_operations (
    origin uuid NOT NULL,
    kid uuid NOT NULL,
    cluster_id uuid NOT NULL,
    node_id uuid NOT NULL,
    revision bigint NOT NULL,
    model_id uuid NOT NULL,
    deployment_id uuid NOT NULL,
    credential_id uuid NOT NULL,
    credential_version integer NOT NULL,
    upstream_version integer NOT NULL,
    requested_handle uuid,
    handle uuid,
    preloaded boolean DEFAULT false NOT NULL,
    memory_tier text NOT NULL,
    created_at timestamptz DEFAULT clock_timestamp() NOT NULL,
    cleanup boolean DEFAULT false NOT NULL,
    CONSTRAINT container_operations_pkey PRIMARY KEY (origin)
);

CREATE TABLE publication.conversations (
    id uuid NOT NULL,
    api_key_id uuid NOT NULL,
    cluster_id uuid NOT NULL,
    model_id uuid NOT NULL,
    deployment_id uuid NOT NULL,
    credential_id uuid NOT NULL,
    credential_version integer NOT NULL,
    upstream_version integer NOT NULL,
    provider_id text NOT NULL,
    deleted_at timestamptz,
    created_at timestamptz DEFAULT clock_timestamp() NOT NULL,
    CONSTRAINT conversations_provider_id_check CHECK ((provider_id ~ '^[A-Za-z0-9_-]{1,200}$'::text)),
    CONSTRAINT conversations_pkey PRIMARY KEY (id),
    CONSTRAINT conversations_cluster_id_api_key_id_deployment_id_provider__key UNIQUE (cluster_id, api_key_id, deployment_id, provider_id)
);
CREATE INDEX conversations_key_created ON publication.conversations (api_key_id, created_at DESC, id DESC);

CREATE TABLE publication.credential_envelopes (
    cluster_id uuid NOT NULL,
    revision bigint NOT NULL,
    credential_id uuid NOT NULL,
    credential_version integer NOT NULL,
    key_version integer NOT NULL,
    ciphertext bytea NOT NULL,
    CONSTRAINT credential_envelopes_pkey PRIMARY KEY (cluster_id, revision, credential_id, credential_version)
);

CREATE TABLE publication.execution_containers (
    id uuid NOT NULL,
    cluster_id uuid NOT NULL,
    api_key_id uuid NOT NULL,
    model_id uuid NOT NULL,
    deployment_id uuid NOT NULL,
    endpoint text NOT NULL,
    credential_id uuid NOT NULL,
    credential_version integer NOT NULL,
    upstream_version integer NOT NULL,
    provider_id text NOT NULL,
    metadata jsonb DEFAULT '{}' NOT NULL,
    created_at timestamptz DEFAULT clock_timestamp() NOT NULL,
    deleted_at timestamptz,
    CONSTRAINT execution_containers_endpoint_check CHECK ((endpoint = ANY (ARRAY['openai_responses'::text, 'anthropic_messages'::text]))),
    CONSTRAINT execution_containers_provider_id_check CHECK ((provider_id ~ '^[A-Za-z0-9_-]{1,200}$'::text)),
    CONSTRAINT execution_containers_pkey PRIMARY KEY (id),
    CONSTRAINT execution_containers_cluster_id_credential_id_credential_ve_key UNIQUE (cluster_id, credential_id, credential_version, provider_id)
);
CREATE INDEX execution_containers_key_created ON publication.execution_containers (api_key_id, created_at DESC, id DESC);
CREATE INDEX execution_owner ON publication.execution_containers (cluster_id, api_key_id, endpoint, created_at, id) WHERE (deleted_at IS NULL);

CREATE TABLE publication.gateway_clusters (
    id uuid NOT NULL,
    name text NOT NULL,
    region text NOT NULL,
    organization_ids uuid[] NOT NULL,
    deployment_ids uuid[] NOT NULL,
    public_key bytea NOT NULL,
    key_version integer DEFAULT 1 NOT NULL,
    active boolean DEFAULT true NOT NULL,
    desired_revision bigint DEFAULT 0 NOT NULL,
    distributed_revision bigint DEFAULT 0 NOT NULL,
    version integer DEFAULT 1 NOT NULL,
    scope_generation integer DEFAULT 1 NOT NULL,
    organization_scope text DEFAULT 'listed' NOT NULL,
    auto_publish boolean DEFAULT true NOT NULL,
    key_sequence bigint DEFAULT 0 NOT NULL,
    CONSTRAINT gateway_clusters_key_sequence_check CHECK ((key_sequence >= 0)),
    CONSTRAINT gateway_clusters_organization_scope_check CHECK ((organization_scope = ANY (ARRAY['listed'::text, 'all'::text, 'personal'::text]))),
    CONSTRAINT gateway_clusters_public_key_check CHECK ((octet_length(public_key) = 32)),
    CONSTRAINT gateway_clusters_scope CHECK (((cardinality(deployment_ids) > 0) AND ((organization_scope = 'listed'::text) = (cardinality(organization_ids) > 0)))),
    CONSTRAINT gateway_clusters_pkey PRIMARY KEY (id),
    CONSTRAINT gateway_clusters_name_key UNIQUE (name)
);

CREATE TABLE publication.gateway_principals (
    node_id uuid NOT NULL,
    cluster_id uuid NOT NULL,
    database_role name NOT NULL,
    capabilities text[] NOT NULL,
    active boolean DEFAULT true NOT NULL,
    applied_revision bigint DEFAULT 0 NOT NULL,
    applied_at timestamptz,
    CONSTRAINT gateway_principals_pkey PRIMARY KEY (node_id),
    CONSTRAINT gateway_principals_cluster_id_node_id_key UNIQUE (cluster_id, node_id),
    CONSTRAINT gateway_principals_database_role_key UNIQUE (database_role)
);

CREATE TABLE publication.inactive_keys (
    api_key_id uuid NOT NULL,
    organization_id uuid,
    reason text NOT NULL,
    inactive_since timestamptz NOT NULL,
    observed_at timestamptz DEFAULT clock_timestamp() NOT NULL,
    CONSTRAINT inactive_keys_reason_check CHECK ((reason ~ '^[a-z_]{1,64}$'::text)),
    CONSTRAINT inactive_keys_pkey PRIMARY KEY (api_key_id)
);

CREATE TABLE publication.limit_namespaces (
    organization_id uuid NOT NULL,
    CONSTRAINT limit_namespaces_pkey PRIMARY KEY (organization_id)
);

CREATE TABLE publication.message_batches (
    id uuid NOT NULL,
    api_key_id uuid NOT NULL,
    cluster_id uuid NOT NULL,
    owner_node uuid NOT NULL,
    revision bigint NOT NULL,
    model_id uuid NOT NULL,
    deployment_id uuid NOT NULL,
    credential_id uuid NOT NULL,
    credential_version integer NOT NULL,
    upstream_version integer NOT NULL,
    provider_id text,
    headers jsonb NOT NULL,
    created_at timestamptz DEFAULT clock_timestamp() NOT NULL,
    next_poll timestamptz NOT NULL,
    done boolean DEFAULT false NOT NULL,
    deleted boolean DEFAULT false NOT NULL,
    metadata jsonb,
    CONSTRAINT message_batches_provider_id_check CHECK ((provider_id ~ '^[A-Za-z0-9_-]{1,200}$'::text)),
    CONSTRAINT message_batches_pkey PRIMARY KEY (id)
);
CREATE INDEX batch_page ON publication.message_batches (cluster_id, api_key_id, created_at, id) WHERE ((NOT deleted) AND (provider_id IS NOT NULL));
CREATE INDEX batch_poll ON publication.message_batches (owner_node, next_poll) WHERE (NOT done);
CREATE INDEX message_batch_owner ON publication.message_batches (cluster_id, api_key_id) WHERE (NOT deleted);
CREATE INDEX message_batches_key_created ON publication.message_batches (api_key_id, created_at DESC, id DESC);

CREATE TABLE publication.node_config_acks (
    node_id uuid NOT NULL,
    cluster_id uuid NOT NULL,
    revision bigint NOT NULL,
    digest text NOT NULL,
    applied boolean NOT NULL,
    error_code text,
    created_at timestamptz DEFAULT clock_timestamp()
);

CREATE TABLE publication.previous_keys (
    cluster_id uuid NOT NULL,
    key_version integer NOT NULL,
    valid_until timestamptz NOT NULL,
    CONSTRAINT previous_keys_pkey PRIMARY KEY (cluster_id, key_version)
);

CREATE TABLE publication.purge_request_items (
    request_id uuid NOT NULL,
    purge_id uuid NOT NULL,
    CONSTRAINT purge_request_items_pkey PRIMARY KEY (request_id, purge_id)
);

CREATE TABLE publication.purge_requests (
    id uuid NOT NULL,
    organization_id uuid NOT NULL,
    actor_id uuid NOT NULL,
    api_key_id uuid NOT NULL,
    kind text,
    resource_id uuid,
    key_digest text NOT NULL,
    parameters text NOT NULL,
    created_at timestamptz DEFAULT clock_timestamp() NOT NULL,
    CONSTRAINT purge_requests_check CHECK (((kind IS NULL) = (resource_id IS NULL))),
    CONSTRAINT purge_requests_kind_check CHECK ((kind = ANY (ARRAY['response'::text, 'file'::text, 'conversation'::text, 'vector_store'::text, 'container'::text, 'batch'::text]))),
    CONSTRAINT purge_requests_pkey PRIMARY KEY (id),
    CONSTRAINT purge_requests_organization_id_actor_id_key_digest_key UNIQUE (organization_id, actor_id, key_digest)
);

CREATE TABLE publication.release_commands (
    cluster_id uuid NOT NULL,
    actor_id uuid NOT NULL,
    key_digest text NOT NULL,
    parameters text NOT NULL,
    revision bigint NOT NULL,
    CONSTRAINT release_commands_pkey PRIMARY KEY (cluster_id, actor_id, key_digest)
);

CREATE TABLE publication.response_jobs (
    handle uuid NOT NULL,
    rid uuid NOT NULL,
    aid uuid NOT NULL,
    owner_node uuid NOT NULL,
    revision bigint NOT NULL,
    attempt_event uuid NOT NULL,
    close_event uuid NOT NULL,
    created_at timestamptz DEFAULT clock_timestamp() NOT NULL,
    next_poll timestamptz DEFAULT clock_timestamp() NOT NULL,
    done boolean DEFAULT false NOT NULL,
    CONSTRAINT response_jobs_pkey PRIMARY KEY (handle),
    CONSTRAINT response_jobs_rid_key UNIQUE (rid)
);
CREATE INDEX response_jobs_pending ON publication.response_jobs (owner_node, next_poll) WHERE (NOT done);

CREATE TABLE publication.snapshot_index (
    cluster_id uuid NOT NULL,
    revision bigint NOT NULL,
    kind text NOT NULL,
    primary_id uuid NOT NULL,
    secondary_id uuid NOT NULL,
    body jsonb NOT NULL,
    CONSTRAINT snapshot_index_kind_check CHECK ((kind = ANY (ARRAY['route'::text, 'deployment'::text, 'model'::text, 'access'::text]))),
    CONSTRAINT snapshot_index_pkey PRIMARY KEY (cluster_id, revision, kind, primary_id, secondary_id)
);

CREATE TABLE publication.stored_responses (
    id uuid NOT NULL,
    request_id uuid NOT NULL,
    api_key_id uuid NOT NULL,
    cluster_id uuid NOT NULL,
    model_id uuid NOT NULL,
    deployment_id uuid NOT NULL,
    credential_id uuid NOT NULL,
    credential_version integer NOT NULL,
    upstream_version integer NOT NULL,
    upstream_model text NOT NULL,
    provider_id text NOT NULL,
    deleted_at timestamptz,
    created_at timestamptz DEFAULT clock_timestamp() NOT NULL,
    CONSTRAINT stored_responses_provider_id_check CHECK ((provider_id ~ '^[A-Za-z0-9_-]{1,200}$'::text)),
    CONSTRAINT stored_responses_pkey PRIMARY KEY (id),
    CONSTRAINT stored_responses_request_id_key UNIQUE (request_id)
);
CREATE INDEX stored_responses_key_created ON publication.stored_responses (api_key_id, created_at DESC, id DESC);

CREATE TABLE publication.tenant_files (
    id uuid NOT NULL,
    api_key_id uuid NOT NULL,
    cluster_id uuid NOT NULL,
    model_id uuid NOT NULL,
    deployment_id uuid NOT NULL,
    credential_id uuid NOT NULL,
    credential_version integer NOT NULL,
    upstream_version integer NOT NULL,
    provider_id text NOT NULL,
    metadata jsonb NOT NULL,
    created_at timestamptz DEFAULT clock_timestamp() NOT NULL,
    deleted_at timestamptz,
    endpoint text DEFAULT 'anthropic_messages' NOT NULL,
    expires_at timestamptz,
    CONSTRAINT tenant_files_endpoint_check CHECK ((endpoint = ANY (ARRAY['anthropic_messages'::text, 'openai_responses'::text]))),
    CONSTRAINT tenant_files_provider_id_check CHECK ((provider_id ~ '^[A-Za-z0-9_-]{1,200}$'::text)),
    CONSTRAINT tenant_files_pkey PRIMARY KEY (id)
);
CREATE INDEX tenant_file_page ON publication.tenant_files (cluster_id, api_key_id, created_at, id) WHERE (deleted_at IS NULL);
CREATE INDEX tenant_files_key_created ON publication.tenant_files (api_key_id, created_at DESC, id DESC);
CREATE INDEX tenant_files_protocol_page ON publication.tenant_files (cluster_id, api_key_id, endpoint, created_at, id) WHERE (deleted_at IS NULL);
CREATE INDEX tenant_files_upstream_id ON publication.tenant_files (cluster_id, api_key_id, deployment_id, provider_id);

CREATE TABLE publication.upstream_purges (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    cluster_id uuid NOT NULL,
    kind text NOT NULL,
    resource_id uuid,
    api_key_id uuid NOT NULL,
    organization_id uuid,
    deployment_id uuid NOT NULL,
    credential_id uuid NOT NULL,
    credential_version integer NOT NULL,
    endpoint text NOT NULL,
    provider_id text NOT NULL,
    reason text NOT NULL,
    state text DEFAULT 'pending' NOT NULL,
    attempts integer DEFAULT 0 NOT NULL,
    next_attempt timestamptz DEFAULT clock_timestamp() NOT NULL,
    claimed_by uuid,
    last_error text,
    created_at timestamptz DEFAULT clock_timestamp() NOT NULL,
    finished_at timestamptz,
    CONSTRAINT upstream_purges_endpoint_check CHECK ((endpoint = ANY (ARRAY['openai_responses'::text, 'anthropic_messages'::text]))),
    CONSTRAINT upstream_purges_kind_check CHECK ((kind = ANY (ARRAY['response'::text, 'file'::text, 'conversation'::text, 'vector_store'::text, 'container'::text, 'batch'::text]))),
    CONSTRAINT upstream_purges_last_error_check CHECK ((last_error ~ '^[a-z0-9_]{1,64}$'::text)),
    CONSTRAINT upstream_purges_provider_id_check CHECK ((provider_id ~ '^[A-Za-z0-9_-]{1,200}$'::text)),
    CONSTRAINT upstream_purges_reason_check CHECK ((reason = ANY (ARRAY['key_inactive'::text, 'orphan'::text, 'owner_request'::text]))),
    CONSTRAINT upstream_purges_state_check CHECK ((state = ANY (ARRAY['pending'::text, 'done'::text, 'abandoned'::text]))),
    CONSTRAINT upstream_purges_pkey PRIMARY KEY (id),
    CONSTRAINT upstream_purges_cluster_id_kind_deployment_id_provider_id_key UNIQUE (cluster_id, kind, deployment_id, provider_id)
);
CREATE INDEX upstream_purge_key ON publication.upstream_purges (api_key_id, created_at DESC, id DESC);
CREATE INDEX upstream_purge_resource ON publication.upstream_purges (resource_id) WHERE (resource_id IS NOT NULL);
CREATE INDEX upstream_purges_due ON publication.upstream_purges (cluster_id, next_attempt) WHERE (state = 'pending'::text);

CREATE TABLE publication.vector_file_batches (
    id uuid NOT NULL,
    store_id uuid NOT NULL,
    provider_id text NOT NULL,
    CONSTRAINT vector_file_batches_provider_id_check CHECK ((provider_id ~ '^[A-Za-z0-9_-]{1,200}$'::text)),
    CONSTRAINT vector_file_batches_pkey PRIMARY KEY (id),
    CONSTRAINT vector_file_batches_store_id_provider_id_key UNIQUE (store_id, provider_id)
);

CREATE TABLE publication.vector_stores (
    id uuid NOT NULL,
    cluster_id uuid NOT NULL,
    api_key_id uuid NOT NULL,
    owner_node uuid NOT NULL,
    poll_node uuid,
    model_id uuid NOT NULL,
    deployment_id uuid NOT NULL,
    credential_id uuid NOT NULL,
    credential_version integer NOT NULL,
    upstream_version integer NOT NULL,
    provider_id text,
    state text DEFAULT 'submitting' NOT NULL,
    metadata jsonb,
    provider_created_at timestamptz,
    created_at timestamptz DEFAULT clock_timestamp() NOT NULL,
    observed_at timestamptz,
    next_poll timestamptz DEFAULT (clock_timestamp() + '00:02:00'::interval) NOT NULL,
    CONSTRAINT vector_stores_provider_id_check CHECK ((provider_id ~ '^[A-Za-z0-9_-]{1,200}$'::text)),
    CONSTRAINT vector_stores_state_check CHECK ((state = ANY (ARRAY['submitting'::text, 'active'::text, 'expired'::text, 'deleted'::text, 'unknown'::text, 'rejected'::text]))),
    CONSTRAINT vector_stores_pkey PRIMARY KEY (id),
    CONSTRAINT vector_stores_cluster_id_api_key_id_deployment_id_provider__key UNIQUE (cluster_id, api_key_id, deployment_id, provider_id)
);
CREATE INDEX vector_store_owner ON publication.vector_stores (cluster_id, api_key_id, provider_created_at, id);
CREATE INDEX vector_store_poll ON publication.vector_stores (next_poll) WHERE (state = ANY (ARRAY['submitting'::text, 'active'::text]));
CREATE INDEX vector_stores_key_created ON publication.vector_stores (api_key_id, created_at DESC, id DESC);
