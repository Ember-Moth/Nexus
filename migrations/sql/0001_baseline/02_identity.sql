-- identity: users, sessions, organizations, projects, service accounts, API keys, access
-- policies, mail and the identity policies.
-- Tables with their keys and checks, then their indexes. Foreign keys are in 07_foreign_keys.sql.

CREATE TABLE identity.access_policies (
    id uuid NOT NULL,
    version integer DEFAULT 1 NOT NULL,
    created_at timestamptz DEFAULT now() NOT NULL,
    organization_id uuid NOT NULL,
    layer varchar(20) NOT NULL,
    scope_type varchar(20) NOT NULL,
    scope_id uuid NOT NULL,
    project_id uuid,
    api_key_id uuid,
    CONSTRAINT access_policy_layer CHECK (((layer IN ('platform', 'customer')) AND (version > 0))),
    CONSTRAINT access_policy_scope CHECK (((((scope_type)::text = 'organization'::text) AND (scope_id = organization_id) AND (project_id IS NULL) AND (api_key_id IS NULL)) OR (((scope_type)::text = 'project'::text) AND (scope_id = project_id) AND (project_id IS NOT NULL) AND (api_key_id IS NULL)) OR (((scope_type)::text = 'key'::text) AND (scope_id = api_key_id) AND (api_key_id IS NOT NULL) AND (project_id IS NOT NULL)))),
    CONSTRAINT access_policies_pkey PRIMARY KEY (id),
    CONSTRAINT access_policy_scope_unique UNIQUE (organization_id, layer, scope_type, scope_id)
);
CREATE INDEX access_policies_org_page_idx ON identity.access_policies (organization_id, created_at, id);

CREATE TABLE identity.access_policy_models (
    policy_id uuid NOT NULL,
    model_id uuid NOT NULL,
    CONSTRAINT access_policy_models_pkey PRIMARY KEY (policy_id, model_id)
);

CREATE TABLE identity.admin_sessions (
    id uuid NOT NULL,
    user_id uuid NOT NULL,
    token_digest varchar(64) NOT NULL,
    created_at timestamptz DEFAULT now() NOT NULL,
    expires_at timestamptz NOT NULL,
    revoked_at timestamptz,
    CONSTRAINT admin_sessions_expiry CHECK ((expires_at > created_at)),
    CONSTRAINT admin_sessions_pkey PRIMARY KEY (id),
    CONSTRAINT admin_sessions_token_digest_key UNIQUE (token_digest)
);
CREATE INDEX admin_sessions_user_idx ON identity.admin_sessions (user_id);

CREATE TABLE identity.api_keys (
    id uuid NOT NULL,
    organization_id uuid NOT NULL,
    version integer DEFAULT 1 NOT NULL,
    created_at timestamptz DEFAULT now() NOT NULL,
    project_id uuid NOT NULL,
    service_account_id uuid NOT NULL,
    name varchar(200) NOT NULL,
    digest varchar(64) NOT NULL,
    prefix varchar(16) NOT NULL,
    expires_at timestamptz,
    revoked_at timestamptz,
    rotated_at timestamptz,
    rotated_from_id uuid,
    CONSTRAINT api_key_fields CHECK (((version > 0) AND (length(btrim((name)::text)) > 0))),
    CONSTRAINT api_keys_pkey PRIMARY KEY (id),
    CONSTRAINT api_key_policy_scope_unique UNIQUE (organization_id, project_id, id),
    CONSTRAINT api_key_rotation_scope_unique UNIQUE (organization_id, project_id, service_account_id, id),
    CONSTRAINT api_key_tenant_unique UNIQUE (organization_id, id),
    CONSTRAINT api_keys_digest_key UNIQUE (digest),
    CONSTRAINT api_keys_rotated_from_id_key UNIQUE (rotated_from_id)
);
CREATE INDEX api_keys_tenant_page_idx ON identity.api_keys (organization_id, created_at, id);

CREATE TABLE identity.audit_events (
    id uuid NOT NULL,
    actor_type varchar(20) NOT NULL,
    action varchar(100) NOT NULL,
    resource_id uuid NOT NULL,
    details jsonb NOT NULL,
    created_at timestamptz DEFAULT now() NOT NULL,
    actor_id uuid,
    organization_id uuid,
    trace_id uuid DEFAULT (NULLIF(current_setting('nexus.request_id'::text, true), ''::text))::uuid,
    CONSTRAINT audit_events_pkey PRIMARY KEY (id)
);
CREATE INDEX audit_events_created_at_idx ON identity.audit_events (created_at, id);
CREATE INDEX audit_trace_idx ON identity.audit_events (trace_id) WHERE (trace_id IS NOT NULL);

CREATE TABLE identity.auth_policy (
    singleton boolean DEFAULT true NOT NULL,
    email_verification boolean DEFAULT false NOT NULL,
    turnstile boolean DEFAULT false NOT NULL,
    version integer DEFAULT 1 NOT NULL,
    registration boolean DEFAULT false NOT NULL,
    login_window_seconds integer DEFAULT 60 NOT NULL,
    login_per_account integer DEFAULT 10 NOT NULL,
    login_per_ip integer DEFAULT 60 NOT NULL,
    mail_window_seconds integer DEFAULT 3600 NOT NULL,
    mail_per_recipient integer DEFAULT 3 NOT NULL,
    mail_per_recipient_total integer DEFAULT 10 NOT NULL,
    mail_per_account integer DEFAULT 10 NOT NULL,
    mail_per_ip integer DEFAULT 20 NOT NULL,
    mail_global integer DEFAULT 1000 NOT NULL,
    mail_queue_ttl_seconds integer DEFAULT 3600 NOT NULL,
    session_ttl_seconds integer DEFAULT 28800 NOT NULL,
    password_setup_ttl_seconds integer DEFAULT 86400 NOT NULL,
    reserved_logins text[] DEFAULT ARRAY['abuse'::text, 'admin'::text, 'administrator'::text, 'hostmaster'::text, 'mailer-daemon'::text, 'nexus'::text, 'no-reply'::text, 'noreply'::text, 'operator'::text, 'postmaster'::text, 'root'::text, 'security'::text, 'superuser'::text, 'support'::text, 'sysadmin'::text, 'system'::text, 'webmaster'::text] NOT NULL,
    platform_email_recovery boolean DEFAULT false NOT NULL,
    managed_sha256 text,
    managed_at timestamptz,
    CONSTRAINT auth_policy_login_per_account_check CHECK (((login_per_account >= 1) AND (login_per_account <= 1000))),
    CONSTRAINT auth_policy_login_per_ip_check CHECK (((login_per_ip >= 1) AND (login_per_ip <= 10000))),
    CONSTRAINT auth_policy_login_window_seconds_check CHECK (((login_window_seconds >= 1) AND (login_window_seconds <= 3600))),
    CONSTRAINT auth_policy_mail_global_check CHECK (((mail_global >= 1) AND (mail_global <= 10000000))),
    CONSTRAINT auth_policy_mail_per_account_check CHECK (((mail_per_account >= 1) AND (mail_per_account <= 10000))),
    CONSTRAINT auth_policy_mail_per_ip_check CHECK (((mail_per_ip >= 1) AND (mail_per_ip <= 100000))),
    CONSTRAINT auth_policy_mail_per_recipient_check CHECK (((mail_per_recipient >= 1) AND (mail_per_recipient <= 1000))),
    CONSTRAINT auth_policy_mail_per_recipient_total_check CHECK (((mail_per_recipient_total >= 1) AND (mail_per_recipient_total <= 10000))),
    CONSTRAINT auth_policy_mail_queue_ttl_seconds_check CHECK (((mail_queue_ttl_seconds >= 60) AND (mail_queue_ttl_seconds <= 86400))),
    CONSTRAINT auth_policy_mail_recipient_check CHECK ((mail_per_recipient <= mail_per_recipient_total)),
    CONSTRAINT auth_policy_mail_window_seconds_check CHECK (((mail_window_seconds >= 60) AND (mail_window_seconds <= 86400))),
    CONSTRAINT auth_policy_managed_check CHECK (((managed_sha256 IS NULL) = (managed_at IS NULL))),
    CONSTRAINT auth_policy_managed_sha256_check CHECK ((managed_sha256 ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT auth_policy_password_setup_ttl_seconds_check CHECK (((password_setup_ttl_seconds >= 60) AND (password_setup_ttl_seconds <= 86400))),
    CONSTRAINT auth_policy_registration_check CHECK (((NOT registration) OR email_verification)),
    CONSTRAINT auth_policy_reserved_logins_check CHECK (((cardinality(reserved_logins) <= 1000) AND (array_position(reserved_logins, NULL::text) IS NULL))),
    CONSTRAINT auth_policy_session_ttl_seconds_check CHECK (((session_ttl_seconds >= 60) AND (session_ttl_seconds <= 86400))),
    CONSTRAINT auth_policy_singleton_check CHECK (singleton),
    CONSTRAINT auth_policy_version_check CHECK ((version > 0)),
    CONSTRAINT auth_policy_pkey PRIMARY KEY (singleton)
);

CREATE TABLE identity.email_challenges (
    id uuid NOT NULL,
    purpose text NOT NULL,
    user_id uuid,
    email varchar(254) NOT NULL,
    login varchar(254),
    password_hash varchar(512),
    digest varchar(64) NOT NULL,
    expires_at timestamptz NOT NULL,
    key_version integer NOT NULL,
    ciphertext bytea,
    next_attempt timestamptz DEFAULT now() NOT NULL,
    attempts integer DEFAULT 0 NOT NULL,
    lease_id uuid,
    trace_id uuid DEFAULT (NULLIF(current_setting('nexus.request_id'::text, true), ''::text))::uuid,
    deliver_by timestamptz DEFAULT (now() + '01:00:00'::interval) NOT NULL,
    CONSTRAINT email_challenges_check CHECK (((purpose = ANY (ARRAY['register'::text, 'exists'::text])) = (user_id IS NULL))),
    CONSTRAINT email_challenges_check1 CHECK (((purpose = 'register'::text) = ((login IS NOT NULL) AND (password_hash IS NOT NULL)))),
    CONSTRAINT email_challenges_purpose_check CHECK ((purpose = ANY (ARRAY['register'::text, 'bind'::text, 'reset'::text, 'exists'::text]))),
    CONSTRAINT email_challenges_pkey PRIMARY KEY (id),
    CONSTRAINT email_challenges_digest_key UNIQUE (digest)
);
CREATE INDEX email_dispatch_idx ON identity.email_challenges (next_attempt) WHERE (ciphertext IS NOT NULL);
CREATE INDEX email_purge_idx ON identity.email_challenges ((
CASE
    WHEN (ciphertext IS NULL) THEN expires_at
    ELSE deliver_by
END));
CREATE INDEX email_user_idx ON identity.email_challenges (user_id, purpose);

CREATE TABLE identity.idempotency_records (
    id uuid NOT NULL,
    organization_id uuid NOT NULL,
    actor_id uuid NOT NULL,
    operation varchar(40) NOT NULL,
    key_digest varchar(64) NOT NULL,
    request_digest varchar(64) NOT NULL,
    resource_id uuid NOT NULL,
    created_at timestamptz DEFAULT now() NOT NULL,
    CONSTRAINT idempotency_records_pkey PRIMARY KEY (id),
    CONSTRAINT identity_idempotency_unique UNIQUE (organization_id, actor_id, operation, key_digest)
);

CREATE TABLE identity.inbox (
    consumer varchar(80) NOT NULL,
    event_id uuid NOT NULL,
    organization_id uuid NOT NULL,
    result_id uuid NOT NULL,
    created_at timestamptz DEFAULT now() NOT NULL,
    CONSTRAINT inbox_pkey PRIMARY KEY (consumer, event_id)
);

CREATE TABLE identity.mail_policy (
    singleton boolean DEFAULT true NOT NULL,
    provider text,
    sender varchar(254),
    smtp_host varchar(253),
    smtp_port integer DEFAULT 587 NOT NULL,
    smtp_security text DEFAULT 'starttls' NOT NULL,
    smtp_username varchar(254),
    cloudflare_account_id text,
    credential bytea,
    key_version integer,
    version integer DEFAULT 1 NOT NULL,
    managed_sha256 text,
    managed_at timestamptz,
    CONSTRAINT mail_policy_check CHECK (((managed_sha256 IS NULL) = (managed_at IS NULL))),
    CONSTRAINT mail_policy_cloudflare_account_id_check CHECK ((cloudflare_account_id ~ '^[0-9a-f]{32}$'::text)),
    CONSTRAINT mail_policy_cloudflare_check CHECK (((provider IS NOT DISTINCT FROM 'cloudflare'::text) = (cloudflare_account_id IS NOT NULL))),
    CONSTRAINT mail_policy_credential_check CHECK ((((credential IS NULL) = (key_version IS NULL)) AND CASE provider WHEN 'smtp'::text THEN ((smtp_username IS NULL) = (credential IS NULL)) WHEN 'resend'::text THEN (credential IS NOT NULL) WHEN 'cloudflare'::text THEN (credential IS NOT NULL) ELSE (credential IS NULL) END)),
    CONSTRAINT mail_policy_key_version_check CHECK ((key_version > 0)),
    CONSTRAINT mail_policy_managed_sha256_check CHECK ((managed_sha256 ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT mail_policy_provider_check CHECK ((provider = ANY (ARRAY['smtp'::text, 'resend'::text, 'cloudflare'::text]))),
    CONSTRAINT mail_policy_sender_check CHECK (((provider IS NULL) = (sender IS NULL))),
    CONSTRAINT mail_policy_singleton_check CHECK (singleton),
    CONSTRAINT mail_policy_smtp_check CHECK ((((provider IS NOT DISTINCT FROM 'smtp'::text) = (smtp_host IS NOT NULL)) AND ((provider IS NOT DISTINCT FROM 'smtp'::text) OR (smtp_username IS NULL)))),
    CONSTRAINT mail_policy_smtp_port_check CHECK (((smtp_port >= 1) AND (smtp_port <= 65535))),
    CONSTRAINT mail_policy_smtp_security_check CHECK ((smtp_security = ANY (ARRAY['starttls'::text, 'tls'::text]))),
    CONSTRAINT mail_policy_version_check CHECK ((version > 0)),
    CONSTRAINT mail_policy_pkey PRIMARY KEY (singleton)
);

CREATE TABLE identity.memberships (
    id uuid NOT NULL,
    organization_id uuid NOT NULL,
    user_id uuid NOT NULL,
    role varchar(32) NOT NULL,
    status varchar(20) DEFAULT 'active' NOT NULL,
    version integer DEFAULT 1 NOT NULL,
    created_at timestamptz DEFAULT now() NOT NULL,
    CONSTRAINT membership_role CHECK ((role IN ('org_owner', 'org_developer', 'org_finance'))),
    CONSTRAINT membership_status CHECK (((status IN ('active', 'disabled')) AND (version > 0))),
    CONSTRAINT memberships_pkey PRIMARY KEY (id),
    CONSTRAINT membership_tenant_unique UNIQUE (organization_id, id),
    CONSTRAINT membership_user_unique UNIQUE (organization_id, user_id)
);
CREATE INDEX memberships_page_idx ON identity.memberships (created_at, id);
CREATE INDEX memberships_user_idx ON identity.memberships (user_id, organization_id);

CREATE TABLE identity.organization_invitations (
    id uuid NOT NULL,
    organization_id uuid NOT NULL,
    login varchar(254) NOT NULL,
    role varchar(32) NOT NULL,
    invited_by uuid NOT NULL,
    token_digest varchar(64) NOT NULL,
    created_at timestamptz DEFAULT now() NOT NULL,
    expires_at timestamptz NOT NULL,
    revoked_at timestamptz,
    accepted_at timestamptz,
    accepted_by uuid,
    CONSTRAINT organization_invitations_check CHECK ((expires_at > created_at)),
    CONSTRAINT organization_invitations_check1 CHECK (((accepted_by IS NULL) = (accepted_at IS NULL))),
    CONSTRAINT organization_invitations_check2 CHECK (((accepted_at IS NULL) OR (revoked_at IS NULL))),
    CONSTRAINT organization_invitations_role_check CHECK ((role IN ('org_owner', 'org_finance', 'org_developer'))),
    CONSTRAINT organization_invitations_pkey PRIMARY KEY (id),
    CONSTRAINT organization_invitations_token_digest_key UNIQUE (token_digest)
);
CREATE INDEX invitations_page_idx ON identity.organization_invitations (organization_id, created_at, id);
CREATE UNIQUE INDEX invitation_open_login ON identity.organization_invitations (organization_id, login) WHERE ((accepted_at IS NULL) AND (revoked_at IS NULL));

CREATE TABLE identity.organizations (
    id uuid NOT NULL,
    name varchar(200) NOT NULL,
    status varchar(20) DEFAULT 'provisioning' NOT NULL,
    resume_status varchar(20),
    version integer DEFAULT 1 NOT NULL,
    created_at timestamptz DEFAULT now() NOT NULL,
    billing_account_id uuid,
    personal_owner_id uuid,
    CONSTRAINT organization_fields CHECK (((version > 0) AND (length(btrim((name)::text)) > 0))),
    CONSTRAINT organization_resume_status CHECK (((resume_status IS NULL) OR (resume_status IN ('provisioning', 'active')))),
    CONSTRAINT organization_status CHECK ((status IN ('provisioning', 'active', 'suspended', 'archived'))),
    CONSTRAINT personal_org_not_archived CHECK (((personal_owner_id IS NULL) OR ((status)::text <> 'archived'::text))),
    CONSTRAINT organizations_pkey PRIMARY KEY (id),
    CONSTRAINT organizations_personal_owner_id_key UNIQUE (personal_owner_id)
);
CREATE INDEX organizations_page_idx ON identity.organizations (created_at, id);

CREATE TABLE identity.outbox (
    id uuid NOT NULL,
    topic varchar(100) NOT NULL,
    schema_version integer NOT NULL,
    aggregate_id uuid NOT NULL,
    aggregate_version integer NOT NULL,
    payload jsonb NOT NULL,
    created_at timestamptz DEFAULT now() NOT NULL,
    published_at timestamptz,
    CONSTRAINT outbox_pkey PRIMARY KEY (id),
    CONSTRAINT identity_outbox_fact UNIQUE (topic, aggregate_id, aggregate_version)
);

CREATE TABLE identity.password_setup_tokens (
    id uuid NOT NULL,
    user_id uuid NOT NULL,
    token_digest varchar(64) NOT NULL,
    created_at timestamptz DEFAULT now() NOT NULL,
    expires_at timestamptz NOT NULL,
    revoked_at timestamptz,
    CONSTRAINT password_setup_tokens_expiry CHECK ((expires_at > created_at)),
    CONSTRAINT password_setup_tokens_pkey PRIMARY KEY (id),
    CONSTRAINT password_setup_tokens_token_digest_key UNIQUE (token_digest)
);
CREATE INDEX password_setup_tokens_user_idx ON identity.password_setup_tokens (user_id);

CREATE TABLE identity.platform_role_bindings (
    user_id uuid NOT NULL,
    role varchar(32) NOT NULL,
    CONSTRAINT platform_role CHECK ((role IN ('platform_admin', 'platform_finance'))),
    CONSTRAINT platform_role_bindings_pkey PRIMARY KEY (user_id, role)
);

CREATE TABLE identity.project_grants (
    id uuid NOT NULL,
    organization_id uuid NOT NULL,
    version integer DEFAULT 1 NOT NULL,
    created_at timestamptz DEFAULT now() NOT NULL,
    project_id uuid NOT NULL,
    membership_id uuid NOT NULL,
    status varchar(20) DEFAULT 'active' NOT NULL,
    CONSTRAINT project_grant_fields CHECK (((status IN ('active', 'revoked')) AND (version > 0))),
    CONSTRAINT project_grants_pkey PRIMARY KEY (id),
    CONSTRAINT project_grant_unique UNIQUE (organization_id, project_id, membership_id)
);
CREATE INDEX grants_membership_idx ON identity.project_grants (organization_id, membership_id, project_id);
CREATE INDEX project_grants_tenant_page_idx ON identity.project_grants (organization_id, created_at, id);

CREATE TABLE identity.projects (
    id uuid NOT NULL,
    organization_id uuid NOT NULL,
    version integer DEFAULT 1 NOT NULL,
    created_at timestamptz DEFAULT now() NOT NULL,
    name varchar(200) NOT NULL,
    status varchar(20) DEFAULT 'active' NOT NULL,
    CONSTRAINT project_fields CHECK (((status IN ('active', 'disabled', 'archived')) AND (version > 0) AND (length(btrim((name)::text)) > 0))),
    CONSTRAINT projects_pkey PRIMARY KEY (id),
    CONSTRAINT project_name_unique UNIQUE (organization_id, name),
    CONSTRAINT project_tenant_unique UNIQUE (organization_id, id)
);
CREATE INDEX projects_tenant_page_idx ON identity.projects (organization_id, created_at, id);

CREATE TABLE identity.service_accounts (
    id uuid NOT NULL,
    organization_id uuid NOT NULL,
    version integer DEFAULT 1 NOT NULL,
    created_at timestamptz DEFAULT now() NOT NULL,
    project_id uuid NOT NULL,
    name varchar(200) NOT NULL,
    status varchar(20) DEFAULT 'active' NOT NULL,
    CONSTRAINT service_account_fields CHECK (((status IN ('active', 'disabled')) AND (version > 0) AND (length(btrim((name)::text)) > 0))),
    CONSTRAINT service_accounts_pkey PRIMARY KEY (id),
    CONSTRAINT service_account_name_unique UNIQUE (organization_id, project_id, name),
    CONSTRAINT service_account_scope_unique UNIQUE (organization_id, project_id, id)
);
CREATE INDEX service_accounts_tenant_page_idx ON identity.service_accounts (organization_id, created_at, id);

CREATE TABLE identity.site_policy (
    singleton boolean DEFAULT true NOT NULL,
    site_name text DEFAULT 'Nexus' NOT NULL,
    console_url text,
    api_url text,
    contact_email varchar(254),
    setup_completed_at timestamptz,
    version integer DEFAULT 1 NOT NULL,
    managed_sha256 text,
    managed_at timestamptz,
    CONSTRAINT site_policy_check CHECK (((managed_sha256 IS NULL) = (managed_at IS NULL))),
    CONSTRAINT site_policy_api_url_check CHECK (((api_url ~ '^https?://'::text) AND (char_length(api_url) <= 2048))),
    CONSTRAINT site_policy_console_url_check CHECK (((console_url ~ '^https?://'::text) AND (char_length(console_url) <= 2048))),
    CONSTRAINT site_policy_managed_sha256_check CHECK ((managed_sha256 ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT site_policy_singleton_check CHECK (singleton),
    CONSTRAINT site_policy_site_name_check CHECK (((char_length(site_name) >= 1) AND (char_length(site_name) <= 100))),
    CONSTRAINT site_policy_version_check CHECK ((version > 0)),
    CONSTRAINT site_policy_pkey PRIMARY KEY (singleton)
);

CREATE TABLE identity.source_revision (
    id integer NOT NULL,
    revision bigint NOT NULL,
    CONSTRAINT source_revision_id_check CHECK ((id = 1)),
    CONSTRAINT source_revision_pkey PRIMARY KEY (id)
);

CREATE TABLE identity.turnstile_policy (
    singleton boolean DEFAULT true NOT NULL,
    site_key varchar(255),
    hostname varchar(253),
    credential bytea,
    key_version integer,
    version integer DEFAULT 1 NOT NULL,
    managed_sha256 text,
    managed_at timestamptz,
    CONSTRAINT turnstile_policy_check CHECK (((managed_sha256 IS NULL) = (managed_at IS NULL))),
    CONSTRAINT turnstile_policy_credential_check CHECK ((((credential IS NULL) = (key_version IS NULL)) AND ((credential IS NULL) = (site_key IS NULL)) AND ((site_key IS NOT NULL) OR (hostname IS NULL)))),
    CONSTRAINT turnstile_policy_key_version_check CHECK ((key_version > 0)),
    CONSTRAINT turnstile_policy_managed_sha256_check CHECK ((managed_sha256 ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT turnstile_policy_singleton_check CHECK (singleton),
    CONSTRAINT turnstile_policy_version_check CHECK ((version > 0)),
    CONSTRAINT turnstile_policy_pkey PRIMARY KEY (singleton)
);

CREATE TABLE identity.users (
    id uuid NOT NULL,
    login varchar(254) NOT NULL,
    password_hash varchar(512),
    status varchar(20) DEFAULT 'active' NOT NULL,
    created_at timestamptz DEFAULT now() NOT NULL,
    version integer DEFAULT 1 NOT NULL,
    verified_email varchar(254),
    unverified_email varchar(254),
    CONSTRAINT active_password CHECK ((((status)::text <> 'active'::text) OR (password_hash IS NOT NULL))),
    CONSTRAINT normalized_login CHECK ((((login)::text = lower(btrim((login)::text))) AND (length((login)::text) >= 3))),
    CONSTRAINT user_status CHECK ((status IN ('pending', 'active', 'disabled'))),
    CONSTRAINT users_single_email CHECK (((verified_email IS NULL) OR (unverified_email IS NULL))),
    CONSTRAINT users_pkey PRIMARY KEY (id),
    CONSTRAINT users_login_key UNIQUE (login),
    CONSTRAINT users_unverified_email_key UNIQUE (unverified_email),
    CONSTRAINT users_verified_email_key UNIQUE (verified_email)
);
CREATE INDEX users_page_idx ON identity.users (created_at, id);
