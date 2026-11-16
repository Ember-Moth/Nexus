-- billing: accounts and the ledger, funding and payments, prices and discounts, budgets,
-- requests, attempts and usage, reviews and settlement.
-- Tables with their keys and checks, then their indexes. Foreign keys are in 07_foreign_keys.sql.

CREATE TABLE billing.account_checks (
    organization_id uuid NOT NULL,
    checked_at timestamptz DEFAULT '-infinity' NOT NULL,
    differences integer DEFAULT 0 NOT NULL,
    ledger_sequence bigint DEFAULT 0 NOT NULL,
    claim uuid,
    claimed_until timestamptz,
    CONSTRAINT account_checks_pkey PRIMARY KEY (organization_id)
);
CREATE INDEX account_checks_due ON billing.account_checks (checked_at);

CREATE TABLE billing.account_restrictions (
    account_id uuid NOT NULL,
    source_type text NOT NULL,
    source_id uuid NOT NULL,
    created_at timestamptz DEFAULT clock_timestamp() NOT NULL,
    released_at timestamptz,
    CONSTRAINT account_restrictions_pkey PRIMARY KEY (account_id, source_type, source_id)
);

CREATE TABLE billing.accounts (
    id uuid NOT NULL,
    organization_id uuid NOT NULL,
    currency varchar(3) NOT NULL,
    balance numeric(28,12) DEFAULT '0' NOT NULL,
    held_amount numeric(28,12) DEFAULT '0' NOT NULL,
    entry_sequence bigint DEFAULT '0' NOT NULL,
    status varchar(20) DEFAULT 'active' NOT NULL,
    version integer DEFAULT 1 NOT NULL,
    created_at timestamptz DEFAULT now() NOT NULL,
    CONSTRAINT account_funds CHECK (((balance >= held_amount) AND (held_amount >= (0)::numeric) AND (balance <> 'NaN'::numeric) AND (held_amount <> 'NaN'::numeric))),
    CONSTRAINT account_state CHECK (((status IN ('active', 'frozen')) AND (version > 0) AND (entry_sequence >= 0))),
    CONSTRAINT accounts_pkey PRIMARY KEY (id),
    CONSTRAINT accounts_organization_id_key UNIQUE (organization_id),
    CONSTRAINT billing_account_currency UNIQUE (organization_id, id, currency),
    CONSTRAINT billing_account_scope UNIQUE (organization_id, id)
);

CREATE TABLE billing.adjustments (
    id uuid NOT NULL,
    organization_id uuid NOT NULL,
    account_id uuid NOT NULL,
    source_entry_id uuid NOT NULL,
    amount numeric(28,12) NOT NULL,
    currency varchar(3) NOT NULL,
    reason varchar(2000) NOT NULL,
    evidence varchar(2000) NOT NULL,
    status varchar(20) DEFAULT 'draft' NOT NULL,
    created_by uuid NOT NULL,
    posted_by uuid,
    posted_at timestamptz,
    version integer DEFAULT 1 NOT NULL,
    created_at timestamptz DEFAULT now() NOT NULL,
    CONSTRAINT adjustment_state CHECK (((amount > (0)::numeric) AND (amount <> 'NaN'::numeric) AND (status IN ('draft', 'posted')))),
    CONSTRAINT adjustments_pkey PRIMARY KEY (id)
);
CREATE INDEX adjustments_org_page ON billing.adjustments (organization_id, created_at, id);
CREATE INDEX adjustments_source_posted ON billing.adjustments (source_entry_id) WHERE ((status)::text = 'posted'::text);

CREATE TABLE billing.audit_events (
    id uuid NOT NULL,
    organization_id uuid,
    actor_id uuid,
    action varchar(100) NOT NULL,
    resource_id uuid NOT NULL,
    details jsonb NOT NULL,
    created_at timestamptz DEFAULT now() NOT NULL,
    trace_id uuid DEFAULT (NULLIF(current_setting('nexus.request_id'::text, true), ''::text))::uuid,
    CONSTRAINT audit_events_pkey PRIMARY KEY (id)
);
CREATE INDEX attempts_cost_audit ON billing.audit_events (resource_id, created_at, id) WHERE ((action)::text = 'attempt.cost_confirmed'::text);
CREATE INDEX audit_trace_idx ON billing.audit_events (trace_id) WHERE (trace_id IS NOT NULL);

CREATE TABLE billing.budget_holds (
    request_id uuid NOT NULL,
    period_id uuid NOT NULL,
    organization_id uuid NOT NULL,
    reserved numeric(28,12) NOT NULL,
    spent numeric(28,12) NOT NULL,
    CONSTRAINT budget_holds_check CHECK (((reserved >= (0)::numeric) AND (reserved <> 'NaN'::numeric) AND (spent >= (0)::numeric) AND (spent <> 'NaN'::numeric))),
    CONSTRAINT budget_holds_pkey PRIMARY KEY (request_id, period_id)
);

CREATE TABLE billing.budget_periods (
    id uuid NOT NULL,
    created_at timestamptz DEFAULT now() NOT NULL,
    organization_id uuid NOT NULL,
    policy_id uuid NOT NULL,
    policy_version integer NOT NULL,
    period_start timestamptz NOT NULL,
    period_end timestamptz NOT NULL,
    limit_amount numeric(28,12) NOT NULL,
    spent numeric(28,12) DEFAULT '0' NOT NULL,
    reserved numeric(28,12) DEFAULT '0' NOT NULL,
    CONSTRAINT budget_periods_check CHECK (((limit_amount >= (0)::numeric) AND (limit_amount <> 'NaN'::numeric) AND (spent >= (0)::numeric) AND (spent <> 'NaN'::numeric) AND (reserved >= (0)::numeric) AND (reserved <> 'NaN'::numeric))),
    CONSTRAINT budget_periods_check1 CHECK ((period_end > period_start)),
    CONSTRAINT budget_periods_pkey PRIMARY KEY (id),
    CONSTRAINT budget_period_scope UNIQUE (organization_id, id),
    CONSTRAINT budget_periods_policy_id_period_start_key UNIQUE (policy_id, period_start)
);

CREATE TABLE billing.budget_policies (
    id uuid NOT NULL,
    created_at timestamptz DEFAULT now() NOT NULL,
    version integer DEFAULT 1 NOT NULL,
    organization_id uuid NOT NULL,
    layer varchar(20) NOT NULL,
    scope_type varchar(20) NOT NULL,
    scope_id uuid NOT NULL,
    project_id uuid,
    api_key_id uuid,
    currency varchar(3) NOT NULL,
    limit_amount numeric(28,12) NOT NULL,
    period varchar(10) NOT NULL,
    status varchar(20) DEFAULT 'active' NOT NULL,
    CONSTRAINT budget_policies_check CHECK (((layer IN ('platform', 'organization')) AND (scope_type IN ('organization', 'project', 'key')) AND (period IN ('day', 'month')) AND (status IN ('active', 'disabled')))),
    CONSTRAINT budget_policies_limit_amount_check CHECK (((limit_amount >= (0)::numeric) AND (limit_amount <> 'NaN'::numeric))),
    CONSTRAINT budget_scope_fields CHECK (((((scope_type)::text = 'organization'::text) AND (scope_id = organization_id) AND (project_id IS NULL) AND (api_key_id IS NULL)) OR (((scope_type)::text = 'project'::text) AND (scope_id = project_id) AND (project_id IS NOT NULL) AND (api_key_id IS NULL)) OR (((scope_type)::text = 'key'::text) AND (scope_id = api_key_id) AND (project_id IS NOT NULL) AND (api_key_id IS NOT NULL)))),
    CONSTRAINT budget_policies_pkey PRIMARY KEY (id),
    CONSTRAINT budget_policies_organization_id_layer_scope_type_scope_id_p_key UNIQUE (organization_id, layer, scope_type, scope_id, period),
    CONSTRAINT budget_policy_scope UNIQUE (organization_id, id)
);

CREATE TABLE billing.case_decisions (
    id uuid NOT NULL,
    case_id uuid NOT NULL,
    organization_id uuid NOT NULL,
    actor_id uuid NOT NULL,
    action text NOT NULL,
    reason text NOT NULL,
    evidence text NOT NULL,
    parameters jsonb NOT NULL,
    result jsonb NOT NULL,
    created_at timestamptz DEFAULT clock_timestamp() NOT NULL,
    CONSTRAINT case_decisions_pkey PRIMARY KEY (id)
);

CREATE TABLE billing.container_extensions (
    request_id uuid NOT NULL,
    meter uuid NOT NULL,
    origin uuid NOT NULL,
    sequence integer NOT NULL,
    start_minute integer NOT NULL,
    minutes integer NOT NULL,
    attempt_id uuid DEFAULT gen_random_uuid() NOT NULL,
    created_at timestamptz DEFAULT clock_timestamp() NOT NULL,
    CONSTRAINT container_extensions_minutes_check CHECK ((minutes > 0)),
    CONSTRAINT container_extensions_sequence_check CHECK ((sequence > 0)),
    CONSTRAINT container_extensions_start_minute_check CHECK ((start_minute >= 0)),
    CONSTRAINT container_extensions_pkey PRIMARY KEY (request_id),
    CONSTRAINT container_extensions_attempt_id_key UNIQUE (attempt_id),
    CONSTRAINT container_extensions_meter_sequence_key UNIQUE (meter, sequence)
);
CREATE INDEX container_extension_meter ON billing.container_extensions (meter);

CREATE TABLE billing.container_operations (
    origin uuid NOT NULL,
    meter uuid NOT NULL,
    api_key_id uuid NOT NULL,
    requested_handle uuid,
    deployment_id uuid NOT NULL,
    provisional boolean NOT NULL,
    finished boolean DEFAULT false NOT NULL,
    created_at timestamptz DEFAULT clock_timestamp() NOT NULL,
    CONSTRAINT container_operations_pkey PRIMARY KEY (origin)
);

CREATE TABLE billing.container_sessions (
    id uuid NOT NULL,
    handle uuid NOT NULL,
    organization_id uuid NOT NULL,
    api_key_id uuid NOT NULL,
    model_id uuid NOT NULL,
    deployment_id uuid NOT NULL,
    supplier_id uuid NOT NULL,
    node_id uuid NOT NULL,
    cluster_id uuid NOT NULL,
    revision bigint NOT NULL,
    attempt_id uuid NOT NULL,
    cost_price_id uuid NOT NULL,
    memory_tier text NOT NULL,
    tariff text NOT NULL,
    state text NOT NULL,
    created_at timestamptz DEFAULT clock_timestamp() NOT NULL,
    last_seen timestamptz DEFAULT clock_timestamp() NOT NULL,
    review_after timestamptz NOT NULL,
    explicit_creation boolean NOT NULL,
    tier_verified boolean DEFAULT false NOT NULL,
    completed_call boolean DEFAULT false NOT NULL,
    closed_at timestamptz,
    provider_id text,
    covered_minutes integer DEFAULT 0 NOT NULL,
    potential_end timestamptz NOT NULL,
    idle_minutes integer DEFAULT 20 NOT NULL,
    CONSTRAINT container_sessions_covered_minutes_check CHECK ((covered_minutes >= 0)),
    CONSTRAINT container_sessions_idle_minutes_check CHECK (((idle_minutes >= 1) AND (idle_minutes <= 1440))),
    CONSTRAINT container_sessions_state_check CHECK ((state = ANY (ARRAY['provisional'::text, 'active'::text, 'review'::text, 'released'::text, 'settled'::text]))),
    CONSTRAINT container_sessions_pkey PRIMARY KEY (id),
    CONSTRAINT container_sessions_attempt_id_key UNIQUE (attempt_id)
);
CREATE INDEX container_idle_meter ON billing.container_sessions (review_after) WHERE (state = ANY (ARRAY['provisional'::text, 'active'::text]));
CREATE UNIQUE INDEX container_open_meter ON billing.container_sessions (handle) WHERE (state = ANY (ARRAY['provisional'::text, 'active'::text, 'review'::text]));

CREATE TABLE billing.cost_corrections (
    id uuid NOT NULL,
    attempt_id uuid NOT NULL,
    decision_id uuid NOT NULL,
    amount numeric(28,12) NOT NULL,
    quantities jsonb NOT NULL,
    cost_price_version_id uuid NOT NULL,
    created_at timestamptz DEFAULT clock_timestamp() NOT NULL,
    CONSTRAINT cost_corrections_amount_check CHECK (((amount >= (0)::numeric) AND (amount <> 'NaN'::numeric))),
    CONSTRAINT cost_corrections_pkey PRIMARY KEY (id),
    CONSTRAINT cost_corrections_decision_id_key UNIQUE (decision_id)
);

CREATE TABLE billing.discount_versions (
    id uuid NOT NULL,
    created_at timestamptz DEFAULT now() NOT NULL,
    version integer DEFAULT 1 NOT NULL,
    organization_id uuid,
    factor numeric(7,6) NOT NULL,
    effective_at timestamptz,
    published_at timestamptz,
    state varchar(20) DEFAULT 'draft' NOT NULL,
    created_by uuid,
    CONSTRAINT discount_versions_check CHECK (((factor > (0)::numeric) AND (factor <= (1)::numeric) AND (factor <> 'NaN'::numeric) AND (state IN ('draft', 'published')) AND (((state)::text <> 'published'::text) OR ((effective_at IS NOT NULL) AND (published_at IS NOT NULL))))),
    CONSTRAINT discount_versions_pkey PRIMARY KEY (id)
);
CREATE UNIQUE INDEX discount_default ON billing.discount_versions (((organization_id IS NULL))) WHERE (organization_id IS NULL);
CREATE UNIQUE INDEX discount_scope_start ON billing.discount_versions (organization_id, effective_at) WHERE ((state)::text = 'published'::text);

CREATE TABLE billing.execution_usage_receipts (
    request_id uuid NOT NULL,
    deployment_id uuid NOT NULL,
    supplier_id uuid NOT NULL,
    reference_digest varchar(64) NOT NULL,
    confirmed_seconds bigint NOT NULL,
    CONSTRAINT execution_usage_receipts_confirmed_seconds_check CHECK ((confirmed_seconds >= 0)),
    CONSTRAINT execution_usage_receipts_pkey PRIMARY KEY (request_id),
    CONSTRAINT execution_usage_receipts_supplier_id_reference_digest_key UNIQUE (supplier_id, reference_digest)
);

CREATE TABLE billing.funding_records (
    id uuid NOT NULL,
    organization_id uuid NOT NULL,
    account_id uuid NOT NULL,
    receiving_account_id uuid,
    receipt_namespace varchar(160) NOT NULL,
    external_reference varchar(255) NOT NULL,
    amount numeric(28,12) NOT NULL,
    currency varchar(3) NOT NULL,
    received_at timestamptz NOT NULL,
    evidence varchar(2000) NOT NULL,
    status varchar(20) DEFAULT 'draft' NOT NULL,
    created_by uuid,
    posted_by uuid,
    posted_at timestamptz,
    version integer DEFAULT 1 NOT NULL,
    created_at timestamptz DEFAULT now() NOT NULL,
    source_type varchar(20) DEFAULT 'manual' NOT NULL,
    system_actor varchar(80),
    payment_order_id uuid,
    payment_receipt_id uuid,
    CONSTRAINT funding_amount CHECK (((amount > (0)::numeric) AND (amount <> 'NaN'::numeric))),
    CONSTRAINT funding_source_actor CHECK (((((source_type)::text = 'manual'::text) AND (receiving_account_id IS NOT NULL) AND (created_by IS NOT NULL) AND (system_actor IS NULL) AND (payment_order_id IS NULL) AND (payment_receipt_id IS NULL)) OR (((source_type)::text = 'gateway'::text) AND (receiving_account_id IS NULL) AND (created_by IS NULL) AND (posted_by IS NULL) AND (system_actor IS NOT NULL) AND ((system_actor)::text = 'payment-worker'::text) AND (payment_order_id IS NOT NULL) AND (payment_receipt_id IS NOT NULL)))),
    CONSTRAINT funding_state CHECK ((status IN ('draft', 'posted', 'cancelled'))),
    CONSTRAINT funding_records_pkey PRIMARY KEY (id),
    CONSTRAINT funding_account_scope UNIQUE (organization_id, account_id, id),
    CONSTRAINT funding_order_unique UNIQUE (payment_order_id),
    CONSTRAINT funding_payment_receipt_unique UNIQUE (payment_receipt_id)
);
CREATE INDEX funding_records_org_page ON billing.funding_records (organization_id, created_at, id);
CREATE UNIQUE INDEX funding_receipt_unique ON billing.funding_records (receipt_namespace, external_reference) WHERE ((status)::text <> 'cancelled'::text);

CREATE TABLE billing.guardrail_usage_receipts (
    attempt_id uuid NOT NULL,
    supplier_id uuid NOT NULL,
    reference_digest varchar(64) NOT NULL,
    quantities jsonb NOT NULL,
    CONSTRAINT guardrail_usage_receipts_pkey PRIMARY KEY (attempt_id),
    CONSTRAINT guardrail_usage_receipts_supplier_id_reference_digest_key UNIQUE (supplier_id, reference_digest)
);

CREATE TABLE billing.idempotency_records (
    id uuid NOT NULL,
    actor_id uuid NOT NULL,
    scope varchar(160) NOT NULL,
    key_digest varchar(64) NOT NULL,
    request_digest varchar(64) NOT NULL,
    resource_kind varchar(40) NOT NULL,
    resource_id uuid NOT NULL,
    created_at timestamptz DEFAULT now() NOT NULL,
    CONSTRAINT idempotency_records_pkey PRIMARY KEY (id),
    CONSTRAINT billing_idempotency_unique UNIQUE (actor_id, scope, key_digest)
);

CREATE TABLE billing.inbox (
    consumer varchar(80) NOT NULL,
    event_id uuid NOT NULL,
    organization_id uuid NOT NULL,
    result_id uuid NOT NULL,
    created_at timestamptz DEFAULT now() NOT NULL,
    CONSTRAINT inbox_pkey PRIMARY KEY (consumer, event_id)
);

CREATE TABLE billing.ingest_counters (
    name text NOT NULL,
    total bigint DEFAULT 0 NOT NULL,
    CONSTRAINT ingest_counters_pkey PRIMARY KEY (name)
);

CREATE TABLE billing.ledger_entries (
    id uuid NOT NULL,
    organization_id uuid NOT NULL,
    account_id uuid NOT NULL,
    sequence bigint NOT NULL,
    currency varchar(3) NOT NULL,
    signed_amount numeric(28,12) NOT NULL,
    balance_after numeric(28,12) NOT NULL,
    kind varchar(40) NOT NULL,
    source_type varchar(30) NOT NULL,
    source_id uuid NOT NULL,
    reverses_entry_id uuid,
    actor_id uuid,
    posted_at timestamptz DEFAULT clock_timestamp() NOT NULL,
    CONSTRAINT ledger_amount CHECK (((signed_amount <> (0)::numeric) AND (signed_amount <> 'NaN'::numeric) AND (balance_after >= (0)::numeric) AND (balance_after <> 'NaN'::numeric))),
    CONSTRAINT ledger_source_direction CHECK (((((kind)::text = 'funding_credit'::text) AND ((source_type)::text = 'funding'::text) AND (signed_amount > (0)::numeric) AND (reverses_entry_id IS NULL)) OR (((kind)::text = 'usage_debit'::text) AND ((source_type)::text = 'request'::text) AND (signed_amount < (0)::numeric) AND (reverses_entry_id IS NULL)) OR (((kind)::text = 'usage_refund'::text) AND ((source_type)::text = 'adjustment'::text) AND (signed_amount > (0)::numeric) AND (reverses_entry_id IS NOT NULL)) OR (((kind)::text = 'funding_reversal'::text) AND ((source_type)::text = 'adjustment'::text) AND (signed_amount < (0)::numeric) AND (reverses_entry_id IS NOT NULL)))),
    CONSTRAINT ledger_entries_pkey PRIMARY KEY (id),
    CONSTRAINT ledger_scope UNIQUE (organization_id, account_id, id),
    CONSTRAINT ledger_sequence_unique UNIQUE (account_id, sequence),
    CONSTRAINT ledger_source_unique UNIQUE (source_type, source_id)
);
CREATE INDEX ledger_reversals ON billing.ledger_entries (reverses_entry_id) WHERE (reverses_entry_id IS NOT NULL);

CREATE TABLE billing.normalized_usage (
    event_id uuid NOT NULL,
    attempt_id uuid NOT NULL,
    rule_version text NOT NULL,
    quantities jsonb,
    review_reason text,
    CONSTRAINT normalized_usage_pkey PRIMARY KEY (event_id)
);

CREATE TABLE billing.outbox (
    id uuid NOT NULL,
    topic varchar(80) NOT NULL,
    schema_version integer NOT NULL,
    organization_id uuid NOT NULL,
    aggregate_id uuid NOT NULL,
    payload jsonb NOT NULL,
    created_at timestamptz DEFAULT now() NOT NULL,
    delivered_at timestamptz,
    CONSTRAINT billing_event_version CHECK ((schema_version > 0)),
    CONSTRAINT outbox_pkey PRIMARY KEY (id),
    CONSTRAINT billing_outbox_fact UNIQUE (topic, aggregate_id)
);

CREATE TABLE billing.payment_cases (
    id uuid NOT NULL,
    created_at timestamptz DEFAULT now() NOT NULL,
    organization_id uuid NOT NULL,
    order_id uuid NOT NULL,
    reason varchar(100) NOT NULL,
    status varchar(20) DEFAULT 'open' NOT NULL,
    amount_at_risk numeric(28,12) DEFAULT '0' NOT NULL,
    evidence jsonb NOT NULL,
    version integer DEFAULT 1 NOT NULL,
    decision_id uuid,
    decision_basis jsonb,
    CONSTRAINT payment_cases_check CHECK (((status IN ('open', 'resolved')) AND (amount_at_risk >= (0)::numeric) AND (amount_at_risk <> 'NaN'::numeric))),
    CONSTRAINT payment_cases_pkey PRIMARY KEY (id),
    CONSTRAINT payment_cases_order_id_reason_key UNIQUE (order_id, reason)
);

CREATE TABLE billing.payment_channel_versions (
    id uuid NOT NULL,
    created_at timestamptz DEFAULT now() NOT NULL,
    channel_id uuid NOT NULL,
    version integer NOT NULL,
    key_version integer NOT NULL,
    ciphertext bytea NOT NULL,
    public_configuration jsonb NOT NULL,
    capabilities jsonb NOT NULL,
    verification_valid_until timestamptz,
    revoked_at timestamptz,
    CONSTRAINT payment_channel_versions_pkey PRIMARY KEY (id),
    CONSTRAINT payment_channel_versions_channel_id_id_key UNIQUE (channel_id, id),
    CONSTRAINT payment_channel_versions_channel_id_version_key UNIQUE (channel_id, version)
);

CREATE TABLE billing.payment_channels (
    id uuid NOT NULL,
    created_at timestamptz DEFAULT now() NOT NULL,
    version integer DEFAULT 1 NOT NULL,
    name varchar(200) NOT NULL,
    adapter_type varchar(32) NOT NULL,
    stable_merchant_id varchar(100) NOT NULL,
    environment varchar(10) NOT NULL,
    receipt_namespace varchar(160) NOT NULL,
    currency varchar(3) NOT NULL,
    min_amount numeric(28,12) NOT NULL,
    max_amount numeric(28,12) NOT NULL,
    status varchar(20) DEFAULT 'active' NOT NULL,
    active_config_id uuid NOT NULL,
    CONSTRAINT payment_channels_check CHECK (((environment IN ('test', 'live')) AND (status IN ('active', 'disabled')) AND (min_amount > (0)::numeric) AND (min_amount <= max_amount) AND (max_amount <> 'NaN'::numeric))),
    CONSTRAINT payment_channels_pkey PRIMARY KEY (id)
);

CREATE TABLE billing.payment_events (
    id uuid NOT NULL,
    created_at timestamptz DEFAULT now() NOT NULL,
    channel_id uuid NOT NULL,
    verification_version_id uuid NOT NULL,
    receipt_namespace varchar(160) NOT NULL,
    order_id uuid,
    operation_id uuid,
    wake_version bigint DEFAULT '0' NOT NULL,
    event_key varchar(320) NOT NULL,
    event_fingerprint varchar(64) NOT NULL,
    body_digest varchar(64) NOT NULL,
    verified_fields jsonb NOT NULL,
    processing_state varchar(20) NOT NULL,
    CONSTRAINT payment_events_processing_state_check CHECK ((processing_state IN ('received', 'ignored', 'conflict', 'processed', 'review'))),
    CONSTRAINT payment_events_pkey PRIMARY KEY (id),
    CONSTRAINT payment_events_receipt_namespace_event_key_event_fingerprin_key UNIQUE (receipt_namespace, event_key, event_fingerprint)
);

CREATE TABLE billing.payment_observations (
    id uuid NOT NULL,
    created_at timestamptz DEFAULT now() NOT NULL,
    order_id uuid NOT NULL,
    config_version_id uuid NOT NULL,
    source varchar(30) NOT NULL,
    order_version integer NOT NULL,
    checkout_generation bigint NOT NULL,
    observed_at timestamptz NOT NULL,
    evidence_digest varchar(64) NOT NULL,
    facts jsonb NOT NULL,
    disposition varchar(30) NOT NULL,
    query_elapsed_ms integer,
    CONSTRAINT payment_observations_pkey PRIMARY KEY (id),
    CONSTRAINT payment_observation_scope UNIQUE (order_id, id)
);

CREATE TABLE billing.payment_operations (
    id uuid NOT NULL,
    created_at timestamptz DEFAULT now() NOT NULL,
    order_id uuid NOT NULL,
    kind varchar(20) NOT NULL,
    operation_key varchar(80) DEFAULT 'main' NOT NULL,
    target_provider_ref varchar(255),
    state varchar(20) DEFAULT 'pending' NOT NULL,
    idempotency_key varchar(80) NOT NULL,
    fencing_token bigint DEFAULT '0' NOT NULL,
    lease_until timestamptz,
    remote_started_at timestamptz,
    retry_observation_id uuid,
    idempotency_mode varchar(30),
    idempotency_expires_at timestamptz,
    attempts integer DEFAULT 0 NOT NULL,
    next_attempt_at timestamptz NOT NULL,
    wake_version bigint DEFAULT '0' NOT NULL,
    claimed_wake_version bigint DEFAULT '0' NOT NULL,
    requested_by uuid,
    last_error varchar(80),
    CONSTRAINT payment_operations_check CHECK (((kind IN ('create', 'query')) AND (state IN ('pending', 'running', 'done', 'review')) AND (fencing_token >= 0) AND (attempts >= 0))),
    CONSTRAINT payment_operations_pkey PRIMARY KEY (id),
    CONSTRAINT payment_operation_scope UNIQUE (order_id, id),
    CONSTRAINT payment_operations_order_id_kind_operation_key_key UNIQUE (order_id, kind, operation_key)
);
CREATE INDEX payment_operations_due ON billing.payment_operations (next_attempt_at, id) WHERE state IN ('pending', 'running');

CREATE TABLE billing.payment_orders (
    id uuid NOT NULL,
    created_at timestamptz DEFAULT now() NOT NULL,
    version integer DEFAULT 1 NOT NULL,
    organization_id uuid NOT NULL,
    account_id uuid NOT NULL,
    channel_id uuid NOT NULL,
    config_version_id uuid NOT NULL,
    merchant_order_ref varchar(80) NOT NULL,
    stable_merchant_id varchar(100) NOT NULL,
    environment varchar(10) NOT NULL,
    receipt_namespace varchar(160) NOT NULL,
    amount numeric(28,12) NOT NULL,
    credit_amount numeric(28,12) NOT NULL,
    currency varchar(3) NOT NULL,
    created_by uuid NOT NULL,
    callback_url varchar(2048) NOT NULL,
    return_url varchar(2048) NOT NULL,
    expires_at timestamptz NOT NULL,
    provider_payment_ref varchar(255),
    checkout_result jsonb,
    checkout_generation bigint DEFAULT '0' NOT NULL,
    checkout_state varchar(20) DEFAULT 'creating' NOT NULL,
    payment_state varchar(30) DEFAULT 'pending' NOT NULL,
    credit_state varchar(20) DEFAULT 'uncredited' NOT NULL,
    review_required boolean DEFAULT false NOT NULL,
    review_reason varchar(100),
    block_credit boolean DEFAULT false NOT NULL,
    reversed_amount numeric(28,12) DEFAULT '0' NOT NULL,
    disputed_amount numeric(28,12) DEFAULT '0' NOT NULL,
    funding_record_id uuid,
    ledger_entry_id uuid,
    credited_at timestamptz,
    last_observed_at timestamptz,
    verified_observation_id uuid,
    collection_amount numeric(28,12) NOT NULL,
    collection_currency varchar(3) NOT NULL,
    exchange_rate numeric(28,12) DEFAULT 1 NOT NULL,
    CONSTRAINT payment_orders_check CHECK (((amount > (0)::numeric) AND (amount <> 'NaN'::numeric) AND (credit_amount = amount) AND (reversed_amount >= (0)::numeric) AND (disputed_amount >= (0)::numeric) AND (reversed_amount <> 'NaN'::numeric) AND (disputed_amount <> 'NaN'::numeric))),
    CONSTRAINT payment_orders_check1 CHECK (((checkout_state IN ('creating', 'ready', 'closed', 'unknown')) AND (payment_state IN ('pending', 'processing', 'succeeded', 'failed', 'cancelled', 'partially_reversed', 'reversed', 'unknown')) AND (credit_state IN ('uncredited', 'credited')) AND (((credit_state)::text <> 'credited'::text) OR ((funding_record_id IS NOT NULL) AND (ledger_entry_id IS NOT NULL) AND (credited_at IS NOT NULL))))),
    CONSTRAINT valid_payment_quote CHECK (((exchange_rate > (0)::numeric) AND (exchange_rate <> 'NaN'::numeric) AND (collection_amount > (0)::numeric) AND ((((currency)::text = (collection_currency)::text) AND (exchange_rate = (1)::numeric) AND (collection_amount = amount)) OR (((currency)::text = 'USD'::text) AND ((collection_currency)::text = 'CNY'::text) AND (collection_amount = round((amount * exchange_rate), 2)))))),
    CONSTRAINT payment_orders_pkey PRIMARY KEY (id),
    CONSTRAINT payment_orders_merchant_order_ref_key UNIQUE (merchant_order_ref),
    CONSTRAINT payment_orders_organization_id_account_id_id_collection_cur_key UNIQUE (organization_id, account_id, id, collection_currency),
    CONSTRAINT payment_orders_organization_id_account_id_id_currency_key UNIQUE (organization_id, account_id, id, currency),
    CONSTRAINT payment_orders_organization_id_account_id_id_key UNIQUE (organization_id, account_id, id)
);
CREATE INDEX payment_orders_namespace ON billing.payment_orders (receipt_namespace, merchant_order_ref);
CREATE INDEX payment_orders_page ON billing.payment_orders (organization_id, created_at, id);

CREATE TABLE billing.payment_rate_buckets (
    receipt_namespace varchar(160) NOT NULL,
    window_start timestamptz NOT NULL,
    request_count integer NOT NULL,
    CONSTRAINT payment_rate_buckets_request_count_check CHECK ((request_count >= 0)),
    CONSTRAINT payment_rate_buckets_pkey PRIMARY KEY (receipt_namespace)
);

CREATE TABLE billing.payment_receipts (
    id uuid NOT NULL,
    created_at timestamptz DEFAULT now() NOT NULL,
    organization_id uuid NOT NULL,
    account_id uuid NOT NULL,
    order_id uuid NOT NULL,
    receipt_namespace varchar(160) NOT NULL,
    transaction_id varchar(255) NOT NULL,
    provider_payment_ref varchar(255) NOT NULL,
    amount numeric(28,12) NOT NULL,
    currency varchar(3) NOT NULL,
    observation_id uuid NOT NULL,
    confirmed_at timestamptz NOT NULL,
    CONSTRAINT payment_receipts_amount_check CHECK (((amount > (0)::numeric) AND (amount <> 'NaN'::numeric))),
    CONSTRAINT payment_receipts_pkey PRIMARY KEY (id),
    CONSTRAINT payment_receipts_organization_id_account_id_order_id_id_key UNIQUE (organization_id, account_id, order_id, id),
    CONSTRAINT payment_receipts_receipt_namespace_transaction_id_key UNIQUE (receipt_namespace, transaction_id)
);

CREATE TABLE billing.price_versions (
    id uuid NOT NULL,
    created_at timestamptz DEFAULT now() NOT NULL,
    version integer DEFAULT 1 NOT NULL,
    kind varchar(10) NOT NULL,
    scope_id uuid NOT NULL,
    model_id uuid NOT NULL,
    currency varchar(3) NOT NULL,
    rates jsonb NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    effective_at timestamptz,
    published_at timestamptz,
    state varchar(20) DEFAULT 'draft' NOT NULL,
    created_by uuid NOT NULL,
    CONSTRAINT price_versions_check CHECK (((kind IN ('sale', 'cost')) AND (state IN ('draft', 'published')) AND (((state)::text <> 'published'::text) OR ((effective_at IS NOT NULL) AND (published_at IS NOT NULL))))),
    CONSTRAINT price_versions_pkey PRIMARY KEY (id)
);
CREATE UNIQUE INDEX price_scope_start ON billing.price_versions (kind, scope_id, effective_at) WHERE ((state)::text = 'published'::text);

CREATE TABLE billing.receiving_accounts (
    id uuid NOT NULL,
    name varchar(200) NOT NULL,
    reference varchar(100) NOT NULL,
    receipt_namespace varchar(80) NOT NULL,
    currency varchar(3) NOT NULL,
    status varchar(20) DEFAULT 'active' NOT NULL,
    version integer DEFAULT 1 NOT NULL,
    created_at timestamptz DEFAULT now() NOT NULL,
    CONSTRAINT receiving_account_state CHECK ((status IN ('active', 'disabled'))),
    CONSTRAINT receiving_accounts_pkey PRIMARY KEY (id),
    CONSTRAINT receiving_accounts_receipt_namespace_key UNIQUE (receipt_namespace),
    CONSTRAINT receiving_accounts_reference_key UNIQUE (reference)
);

CREATE TABLE billing.report_changes (
    request_id uuid NOT NULL,
    generation bigint DEFAULT 1 NOT NULL,
    copied_generation bigint DEFAULT 0 NOT NULL,
    changed_at timestamptz DEFAULT clock_timestamp() NOT NULL,
    CONSTRAINT report_changes_pkey PRIMARY KEY (request_id)
);
CREATE INDEX report_pending ON billing.report_changes (changed_at, request_id) WHERE (generation > copied_generation);

CREATE TABLE billing.request_attempts (
    id uuid NOT NULL,
    created_at timestamptz DEFAULT now() NOT NULL,
    request_id uuid NOT NULL,
    deployment_id uuid NOT NULL,
    cost_price_version_id uuid NOT NULL,
    state varchar(20) DEFAULT 'started' NOT NULL,
    cost_amount numeric(28,12),
    cost_details jsonb,
    usage_digest varchar(64),
    sequence integer,
    expected_usage_format varchar(80),
    meter_supplier_id uuid,
    CONSTRAINT request_attempts_cost_amount_check CHECK (((cost_amount >= (0)::numeric) AND (cost_amount <> 'NaN'::numeric))),
    CONSTRAINT request_attempts_expected_usage_format_check CHECK ((expected_usage_format IN ('openai.responses.v1', 'anthropic.messages.v1', 'aws.bedrock.converse.v1', 'openai.vector_storage.v1', 'openai.container.v1', 'openai.chat.v1'))),
    CONSTRAINT request_attempts_sequence_check CHECK ((sequence > 0)),
    CONSTRAINT request_attempts_state_check CHECK ((state IN ('started', 'confirmed'))),
    CONSTRAINT request_attempts_pkey PRIMARY KEY (id),
    CONSTRAINT request_attempts_request_id_sequence_key UNIQUE (request_id, sequence)
);
CREATE INDEX attempt_request ON billing.request_attempts (request_id);

CREATE TABLE billing.request_runs (
    id uuid NOT NULL,
    created_at timestamptz DEFAULT now() NOT NULL,
    organization_id uuid NOT NULL,
    account_id uuid NOT NULL,
    project_id uuid NOT NULL,
    service_account_id uuid NOT NULL,
    api_key_id uuid NOT NULL,
    model_id uuid NOT NULL,
    request_digest varchar(64) NOT NULL,
    sale_price_version_id uuid NOT NULL,
    discount_version_id uuid NOT NULL,
    billing_policy_version varchar(40) NOT NULL,
    input_limit bigint NOT NULL,
    output_limit bigint NOT NULL,
    reserved_amount numeric(28,12) NOT NULL,
    charged_amount numeric(28,12),
    refunded_amount numeric(28,12) DEFAULT '0' NOT NULL,
    state varchar(30) DEFAULT 'reserved' NOT NULL,
    reservation_state varchar(20) DEFAULT 'held' NOT NULL,
    review_reason varchar(100),
    expires_at timestamptz NOT NULL,
    settled_at timestamptz,
    settlement_digest varchar(64),
    charge_details jsonb,
    node_id uuid,
    cluster_id uuid,
    config_revision bigint,
    review_decision_id uuid,
    web_search_limit integer DEFAULT 0 NOT NULL,
    file_search_limit integer DEFAULT 0 NOT NULL,
    storage_byte_seconds numeric(24,0) DEFAULT 0 NOT NULL,
    execution_mode text DEFAULT 'none' NOT NULL,
    execution_seconds_limit integer DEFAULT 0 NOT NULL,
    container_origin uuid,
    container_handle text,
    container_unit text,
    container_minutes_limit integer DEFAULT 0 NOT NULL,
    container_tariff text,
    guardrail_unit_limit integer DEFAULT 0 NOT NULL,
    CONSTRAINT gateway_request_origin CHECK ((((node_id IS NULL) = (config_revision IS NULL)) AND ((cluster_id IS NULL) = (node_id IS NULL)))),
    CONSTRAINT request_runs_check CHECK (((reserved_amount >= (0)::numeric) AND (reserved_amount <> 'NaN'::numeric) AND (charged_amount >= (0)::numeric) AND (charged_amount <> 'NaN'::numeric) AND (refunded_amount >= (0)::numeric) AND (refunded_amount <> 'NaN'::numeric))),
    CONSTRAINT request_runs_check1 CHECK (((state IN ('reserved', 'running', 'review', 'settled', 'canceled')) AND (reservation_state IN ('held', 'captured', 'released')) AND (input_limit >= 0) AND (output_limit >= 0) AND (refunded_amount <= COALESCE(charged_amount, (0)::numeric)))),
    CONSTRAINT request_runs_container_minutes_limit_check CHECK (((container_minutes_limit >= 0) AND (container_minutes_limit <= 1000000000))),
    CONSTRAINT request_runs_container_tariff_check CHECK ((container_tariff = ANY (ARRAY['session_20m'::text, 'minute_min_5'::text]))),
    CONSTRAINT request_runs_container_unit_check CHECK ((container_unit = ANY (ARRAY['container_1g_minutes'::text, 'container_4g_minutes'::text, 'container_16g_minutes'::text, 'container_64g_minutes'::text]))),
    CONSTRAINT request_runs_execution_mode_check CHECK ((execution_mode = ANY (ARRAY['none'::text, 'bundled'::text, 'metered'::text, 'preloaded'::text]))),
    CONSTRAINT request_runs_execution_seconds_limit_check CHECK (((execution_seconds_limit >= 0) AND (execution_seconds_limit <= 1000000000))),
    CONSTRAINT request_runs_file_search_limit_check CHECK (((file_search_limit >= 0) AND (file_search_limit <= 1000000000))),
    CONSTRAINT request_runs_guardrail_unit_limit_check CHECK (((guardrail_unit_limit >= 0) AND (guardrail_unit_limit <= 1000000000))),
    CONSTRAINT request_runs_storage_byte_seconds_check CHECK ((storage_byte_seconds >= (0)::numeric)),
    CONSTRAINT request_runs_web_search_limit_check CHECK (((web_search_limit >= 0) AND (web_search_limit <= 1000000000))),
    CONSTRAINT request_runs_pkey PRIMARY KEY (id),
    CONSTRAINT request_scope UNIQUE (organization_id, id)
);
CREATE INDEX requests_held_expiry ON billing.request_runs (expires_at) WHERE ((reservation_state)::text = 'held'::text);
CREATE INDEX requests_org_page ON billing.request_runs (organization_id, created_at, id);

CREATE TABLE billing.review_cases (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    organization_id uuid NOT NULL,
    source_type text NOT NULL,
    source_id uuid NOT NULL,
    request_id uuid,
    reason text NOT NULL,
    state text DEFAULT 'open' NOT NULL,
    version integer DEFAULT 1 NOT NULL,
    evidence jsonb DEFAULT '{}' NOT NULL,
    created_at timestamptz DEFAULT clock_timestamp() NOT NULL,
    resolved_at timestamptz,
    resolution_id uuid,
    CONSTRAINT review_cases_source_type_check CHECK ((source_type = ANY (ARRAY['request'::text, 'attempt'::text, 'account'::text]))),
    CONSTRAINT review_cases_state_check CHECK ((state = ANY (ARRAY['open'::text, 'resolved'::text]))),
    CONSTRAINT review_cases_pkey PRIMARY KEY (id),
    CONSTRAINT review_cases_source_type_source_id_key UNIQUE (source_type, source_id)
);
CREATE INDEX review_queue ON billing.review_cases (organization_id, state, created_at, id);

CREATE TABLE billing.settings (
    id serial,
    currency varchar(3) DEFAULT 'USD' NOT NULL,
    created_at timestamptz DEFAULT now() NOT NULL,
    settlement_backlog_limit integer DEFAULT 2048 NOT NULL,
    vector_storage_prepaid_days integer DEFAULT 7 NOT NULL,
    payment_requests_per_merchant_per_minute integer DEFAULT 120 NOT NULL,
    payment_notification_per_channel integer DEFAULT 1000 NOT NULL,
    payment_notification_per_ip integer DEFAULT 120 NOT NULL,
    vector_storage_lapse_policy text DEFAULT 'delete' NOT NULL,
    version integer DEFAULT 1 NOT NULL,
    managed_sha256 text,
    managed_at timestamptz,
    CONSTRAINT billing_settings_singleton CHECK ((id = 1)),
    CONSTRAINT settings_currency_check CHECK (((currency)::text ~ '^[A-Z]{3}$'::text)),
    CONSTRAINT settings_managed_check CHECK (((managed_sha256 IS NULL) = (managed_at IS NULL))),
    CONSTRAINT settings_managed_sha256_check CHECK ((managed_sha256 ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT settings_payment_notification_per_channel_check CHECK (((payment_notification_per_channel >= 1) AND (payment_notification_per_channel <= 100000))),
    CONSTRAINT settings_payment_notification_per_ip_check CHECK (((payment_notification_per_ip >= 1) AND (payment_notification_per_ip <= 10000))),
    CONSTRAINT settings_payment_requests_per_merchant_per_minute_check CHECK (((payment_requests_per_merchant_per_minute >= 1) AND (payment_requests_per_merchant_per_minute <= 10000))),
    CONSTRAINT settings_settlement_backlog_limit_check CHECK ((settlement_backlog_limit > 0)),
    CONSTRAINT settings_vector_storage_lapse_policy_check CHECK ((vector_storage_lapse_policy = ANY (ARRAY['delete'::text, 'suspend'::text]))),
    CONSTRAINT settings_vector_storage_prepaid_days_check CHECK (((vector_storage_prepaid_days >= 1) AND (vector_storage_prepaid_days <= 31))),
    CONSTRAINT settings_version_check CHECK ((version > 0)),
    CONSTRAINT settings_pkey PRIMARY KEY (id)
);

CREATE TABLE billing.settlement_backlog (
    id smallint DEFAULT 1 NOT NULL,
    ready integer DEFAULT 0 NOT NULL,
    observed_at timestamptz DEFAULT '-infinity' NOT NULL,
    CONSTRAINT settlement_backlog_id_check CHECK ((id = 1)),
    CONSTRAINT settlement_backlog_ready_check CHECK ((ready >= 0)),
    CONSTRAINT settlement_backlog_pkey PRIMARY KEY (id)
);

CREATE TABLE billing.source_revision (
    id integer NOT NULL,
    revision bigint NOT NULL,
    CONSTRAINT source_revision_id_check CHECK ((id = 1)),
    CONSTRAINT source_revision_pkey PRIMARY KEY (id)
);

CREATE TABLE billing.upstream_statements (
    id uuid NOT NULL,
    organization_id uuid NOT NULL,
    attempt_id uuid NOT NULL,
    external_reference text NOT NULL,
    currency text NOT NULL,
    amount numeric(28,12) NOT NULL,
    compared_cost numeric(28,12),
    evidence text NOT NULL,
    actor_id uuid NOT NULL,
    created_at timestamptz DEFAULT clock_timestamp() NOT NULL,
    CONSTRAINT upstream_statements_amount_check CHECK (((amount >= (0)::numeric) AND (amount <> 'NaN'::numeric))),
    CONSTRAINT upstream_statements_pkey PRIMARY KEY (id),
    CONSTRAINT upstream_statements_attempt_id_external_reference_key UNIQUE (attempt_id, external_reference)
);

CREATE TABLE billing.usage_events (
    id uuid NOT NULL,
    request_id uuid NOT NULL,
    attempt_id uuid,
    kind text NOT NULL,
    envelope jsonb NOT NULL,
    received_at timestamptz DEFAULT clock_timestamp() NOT NULL,
    CONSTRAINT usage_events_kind_check CHECK ((kind = ANY (ARRAY['attempt.finished'::text, 'request.closed'::text]))),
    CONSTRAINT usage_events_pkey PRIMARY KEY (id)
);
CREATE INDEX usage_by_request ON billing.usage_events (request_id);
CREATE UNIQUE INDEX usage_attempt_finished ON billing.usage_events (attempt_id) WHERE (kind = 'attempt.finished'::text);
CREATE UNIQUE INDEX usage_request_closed ON billing.usage_events (request_id) WHERE (kind = 'request.closed'::text);

CREATE TABLE billing.usage_jobs (
    request_id uuid NOT NULL,
    state text NOT NULL,
    next_attempt_at timestamptz DEFAULT clock_timestamp() NOT NULL,
    failures integer DEFAULT 0 NOT NULL,
    generation bigint DEFAULT 1 NOT NULL,
    reason text,
    CONSTRAINT usage_jobs_state_check CHECK ((state = ANY (ARRAY['ready'::text, 'waiting'::text, 'review'::text, 'done'::text]))),
    CONSTRAINT usage_jobs_pkey PRIMARY KEY (request_id)
);
CREATE INDEX usage_jobs_due ON billing.usage_jobs (next_attempt_at, request_id) WHERE (state = 'ready'::text);

CREATE TABLE billing.vector_storage_contracts (
    id uuid NOT NULL,
    node_id uuid NOT NULL,
    cluster_id uuid NOT NULL,
    revision bigint NOT NULL,
    organization_id uuid NOT NULL,
    project_id uuid NOT NULL,
    service_account_id uuid NOT NULL,
    api_key_id uuid NOT NULL,
    model_id uuid NOT NULL,
    deployment_id uuid NOT NULL,
    sale_id uuid NOT NULL,
    cost_id uuid NOT NULL,
    discount_id uuid NOT NULL,
    segment integer DEFAULT 1 NOT NULL,
    observation_unknown boolean DEFAULT false NOT NULL,
    observed_at timestamptz,
    usage_bytes bigint DEFAULT 0 NOT NULL,
    expires_at timestamptz,
    closed boolean DEFAULT false NOT NULL,
    hold_id uuid,
    prepaid_until timestamptz,
    prepaid_bytes bigint DEFAULT 0 NOT NULL,
    renew_after timestamptz DEFAULT clock_timestamp() NOT NULL,
    renewal_failed_at timestamptz,
    lapsed_at timestamptz,
    lapse_action text,
    CONSTRAINT vector_storage_contracts_check CHECK (((lapsed_at IS NULL) = (lapse_action IS NULL))),
    CONSTRAINT vector_storage_contracts_lapse_action_check CHECK ((lapse_action = ANY (ARRAY['delete'::text, 'suspend'::text]))),
    CONSTRAINT vector_storage_contracts_prepaid_bytes_check CHECK ((prepaid_bytes >= 0)),
    CONSTRAINT vector_storage_contracts_pkey PRIMARY KEY (id)
);
CREATE INDEX vector_storage_renewal ON billing.vector_storage_contracts (renew_after) WHERE (NOT closed);

CREATE TABLE billing.vector_storage_days (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    attempt_id uuid DEFAULT gen_random_uuid() NOT NULL,
    store_id uuid NOT NULL,
    day date NOT NULL,
    segment integer NOT NULL,
    byte_seconds numeric(32,0) DEFAULT 0 NOT NULL,
    covered_until timestamptz NOT NULL,
    uncertain boolean DEFAULT false NOT NULL,
    state text NOT NULL,
    reason text,
    next_attempt timestamptz DEFAULT clock_timestamp() NOT NULL,
    CONSTRAINT vector_storage_days_state_check CHECK ((state = ANY (ARRAY['open'::text, 'ready'::text, 'staged'::text, 'review'::text]))),
    CONSTRAINT vector_storage_days_pkey PRIMARY KEY (id),
    CONSTRAINT vector_storage_days_store_id_day_segment_key UNIQUE (store_id, day, segment)
);
CREATE INDEX vector_storage_ready ON billing.vector_storage_days (next_attempt) WHERE (state = ANY (ARRAY['ready'::text, 'review'::text]));

CREATE TABLE billing.vector_storage_segments (
    day_id uuid NOT NULL,
    started_at timestamptz NOT NULL,
    ended_at timestamptz NOT NULL,
    usage_bytes bigint NOT NULL,
    uncertain boolean NOT NULL,
    CONSTRAINT vector_storage_segments_check CHECK ((ended_at > started_at)),
    CONSTRAINT vector_storage_segments_usage_bytes_check CHECK ((usage_bytes >= 0)),
    CONSTRAINT vector_storage_segments_pkey PRIMARY KEY (day_id, started_at)
);
CREATE INDEX vector_storage_segment_end ON billing.vector_storage_segments (day_id, ended_at);
