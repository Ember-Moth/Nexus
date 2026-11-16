-- catalog: upstreams and their credentials, public models, deployments, route and quota policies.
-- Tables with their keys and checks, then their indexes. Foreign keys are in 07_foreign_keys.sql.

CREATE TABLE catalog.audit_events (
    id uuid NOT NULL,
    actor_id uuid,
    action varchar(100) NOT NULL,
    resource_id uuid NOT NULL,
    details jsonb NOT NULL,
    created_at timestamptz DEFAULT now() NOT NULL,
    organization_id uuid,
    trace_id uuid DEFAULT (NULLIF(current_setting('nexus.request_id'::text, true), ''::text))::uuid,
    CONSTRAINT audit_events_pkey PRIMARY KEY (id)
);
CREATE INDEX audit_trace_idx ON catalog.audit_events (trace_id) WHERE (trace_id IS NOT NULL);

CREATE TABLE catalog.deployments (
    id uuid NOT NULL,
    version integer DEFAULT 1 NOT NULL,
    created_at timestamptz DEFAULT now() NOT NULL,
    name varchar(200) NOT NULL,
    model_id uuid NOT NULL,
    upstream_id uuid NOT NULL,
    credential_id uuid NOT NULL,
    upstream_model varchar(200) NOT NULL,
    region varchar(100) NOT NULL,
    status varchar(20) DEFAULT 'active' NOT NULL,
    relay_profile jsonb,
    CONSTRAINT deployment_status CHECK (((status IN ('active', 'disabled', 'archived')) AND (version > 0))),
    CONSTRAINT deployments_pkey PRIMARY KEY (id),
    CONSTRAINT deployments_name_key UNIQUE (name)
);
CREATE INDEX deployments_model_idx ON catalog.deployments (model_id);
CREATE INDEX deployments_page_idx ON catalog.deployments (created_at, id);

CREATE TABLE catalog.models (
    id uuid NOT NULL,
    version integer DEFAULT 1 NOT NULL,
    created_at timestamptz DEFAULT now() NOT NULL,
    public_name varchar(200) NOT NULL,
    display_name varchar(200) NOT NULL,
    protocols jsonb NOT NULL,
    capabilities jsonb NOT NULL,
    usage_schema varchar(32) NOT NULL,
    token_dimensions jsonb NOT NULL,
    max_input_tokens integer NOT NULL,
    max_output_tokens integer NOT NULL,
    status varchar(20) DEFAULT 'active' NOT NULL,
    tool_dimensions jsonb DEFAULT '[]' NOT NULL,
    CONSTRAINT model_limits CHECK (((max_input_tokens > 0) AND (max_output_tokens > 0))),
    CONSTRAINT model_status CHECK (((status IN ('active', 'disabled', 'archived')) AND (version > 0))),
    CONSTRAINT models_tool_dimensions_check CHECK (((jsonb_typeof(tool_dimensions) = 'array'::text) AND (tool_dimensions <@ '["web_search_requests", "file_search_calls", "vector_storage_byte_seconds", "code_execution_seconds", "container_1g_minutes", "container_4g_minutes", "container_16g_minutes", "container_64g_minutes", "guardrail_content_units", "guardrail_image_units", "guardrail_topic_units", "guardrail_sensitive_units", "guardrail_grounding_units", "guardrail_reasoning_units"]'::jsonb))),
    CONSTRAINT models_pkey PRIMARY KEY (id),
    CONSTRAINT models_public_name_key UNIQUE (public_name)
);
CREATE INDEX models_page_idx ON catalog.models (created_at, id);

CREATE TABLE catalog.quota_policies (
    id uuid NOT NULL,
    organization_id uuid NOT NULL,
    layer text NOT NULL,
    scope_type text NOT NULL,
    scope_id uuid NOT NULL,
    metric text NOT NULL,
    limit_value integer NOT NULL,
    version integer DEFAULT 1 NOT NULL,
    CONSTRAINT quota_policies_count_scope CHECK (((metric = ANY (ARRAY['rpm'::text, 'tpm'::text, 'concurrency'::text])) OR ((layer = 'platform'::text) AND (scope_type = 'organization'::text) AND (scope_id = organization_id)))),
    CONSTRAINT quota_policies_layer_check CHECK ((layer = ANY (ARRAY['platform'::text, 'customer'::text]))),
    CONSTRAINT quota_policies_limit_value_check CHECK ((limit_value > 0)),
    CONSTRAINT quota_policies_metric_check CHECK ((metric = ANY (ARRAY['rpm'::text, 'tpm'::text, 'concurrency'::text, 'admission_concurrency'::text, 'resources_per_key'::text, 'resources_per_organization'::text, 'vector_stores_per_key'::text, 'vector_stores_per_organization'::text]))),
    CONSTRAINT quota_policies_scope_type_check CHECK ((scope_type = ANY (ARRAY['organization'::text, 'project'::text, 'key'::text]))),
    CONSTRAINT quota_policies_pkey PRIMARY KEY (id),
    CONSTRAINT quota_policies_organization_id_layer_scope_type_scope_id_me_key UNIQUE (organization_id, layer, scope_type, scope_id, metric)
);

CREATE TABLE catalog.resource_quota_defaults (
    singleton boolean DEFAULT true NOT NULL,
    resources_per_key integer DEFAULT 10000 NOT NULL,
    resources_per_organization integer DEFAULT 100000 NOT NULL,
    vector_stores_per_key integer DEFAULT 100 NOT NULL,
    vector_stores_per_organization integer DEFAULT 1000 NOT NULL,
    version integer DEFAULT 1 NOT NULL,
    managed_sha256 text,
    managed_at timestamptz,
    CONSTRAINT resource_quota_defaults_check CHECK (((managed_sha256 IS NULL) = (managed_at IS NULL))),
    CONSTRAINT resource_quota_defaults_managed_sha256_check CHECK ((managed_sha256 ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT resource_quota_defaults_resources_per_key_check CHECK (((resources_per_key >= 1) AND (resources_per_key <= 2000000000))),
    CONSTRAINT resource_quota_defaults_resources_per_organization_check CHECK (((resources_per_organization >= 1) AND (resources_per_organization <= 2000000000))),
    CONSTRAINT resource_quota_defaults_singleton_check CHECK (singleton),
    CONSTRAINT resource_quota_defaults_vector_stores_per_key_check CHECK (((vector_stores_per_key >= 1) AND (vector_stores_per_key <= 2000000000))),
    CONSTRAINT resource_quota_defaults_vector_stores_per_organization_check CHECK (((vector_stores_per_organization >= 1) AND (vector_stores_per_organization <= 2000000000))),
    CONSTRAINT resource_quota_defaults_version_check CHECK ((version > 0)),
    CONSTRAINT resource_quota_defaults_pkey PRIMARY KEY (singleton)
);

CREATE TABLE catalog.route_policies (
    organization_id uuid NOT NULL,
    model_id uuid NOT NULL,
    candidates jsonb NOT NULL,
    version integer DEFAULT 1 NOT NULL,
    CONSTRAINT route_policies_pkey PRIMARY KEY (organization_id, model_id)
);

CREATE TABLE catalog.source_revision (
    id integer NOT NULL,
    revision bigint NOT NULL,
    CONSTRAINT source_revision_id_check CHECK ((id = 1)),
    CONSTRAINT source_revision_pkey PRIMARY KEY (id)
);

CREATE TABLE catalog.upstream_credentials (
    id uuid NOT NULL,
    version integer DEFAULT 1 NOT NULL,
    created_at timestamptz DEFAULT now() NOT NULL,
    upstream_id uuid NOT NULL,
    name varchar(200) NOT NULL,
    ciphertext bytea NOT NULL,
    key_version integer NOT NULL,
    status varchar(20) DEFAULT 'active' NOT NULL,
    revoked_at timestamptz,
    CONSTRAINT credential_status CHECK (((status IN ('active', 'revoked')) AND (version > 0) AND (key_version > 0))),
    CONSTRAINT upstream_credentials_pkey PRIMARY KEY (id),
    CONSTRAINT credential_upstream_unique UNIQUE (upstream_id, id)
);
CREATE INDEX upstream_credentials_page_idx ON catalog.upstream_credentials (created_at, id);

CREATE TABLE catalog.upstreams (
    id uuid NOT NULL,
    version integer DEFAULT 1 NOT NULL,
    created_at timestamptz DEFAULT now() NOT NULL,
    name varchar(200) NOT NULL,
    base_url varchar(2048) NOT NULL,
    protocol_family varchar(32) NOT NULL,
    status varchar(20) DEFAULT 'active' NOT NULL,
    CONSTRAINT upstream_status CHECK (((status IN ('active', 'disabled', 'archived')) AND (version > 0))),
    CONSTRAINT upstreams_pkey PRIMARY KEY (id),
    CONSTRAINT upstreams_name_key UNIQUE (name)
);
CREATE INDEX upstreams_page_idx ON catalog.upstreams (created_at, id);
