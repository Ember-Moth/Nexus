-- Foreign keys, added once every table exists: some point across modules (identity to catalog,
-- billing to publication).

-- identity
ALTER TABLE identity.access_policies
    ADD CONSTRAINT access_policies_organization_id_fkey FOREIGN KEY (organization_id) REFERENCES identity.organizations(id);
ALTER TABLE identity.access_policies
    ADD CONSTRAINT access_policies_organization_id_project_id_api_key_id_fkey FOREIGN KEY (organization_id, project_id, api_key_id) REFERENCES identity.api_keys(organization_id, project_id, id);
ALTER TABLE identity.access_policies
    ADD CONSTRAINT access_policies_organization_id_project_id_fkey FOREIGN KEY (organization_id, project_id) REFERENCES identity.projects(organization_id, id);
ALTER TABLE identity.access_policy_models
    ADD CONSTRAINT access_policy_models_model_id_fkey FOREIGN KEY (model_id) REFERENCES catalog.models(id);
ALTER TABLE identity.access_policy_models
    ADD CONSTRAINT access_policy_models_policy_id_fkey FOREIGN KEY (policy_id) REFERENCES identity.access_policies(id) ON DELETE CASCADE;
ALTER TABLE identity.admin_sessions
    ADD CONSTRAINT admin_sessions_user_id_fkey FOREIGN KEY (user_id) REFERENCES identity.users(id);
ALTER TABLE identity.api_keys
    ADD CONSTRAINT api_keys_organization_id_fkey FOREIGN KEY (organization_id) REFERENCES identity.organizations(id);
ALTER TABLE identity.api_keys
    ADD CONSTRAINT api_keys_organization_id_project_id_service_account_id_fkey FOREIGN KEY (organization_id, project_id, service_account_id) REFERENCES identity.service_accounts(organization_id, project_id, id);
ALTER TABLE identity.api_keys
    ADD CONSTRAINT api_keys_organization_id_project_id_service_account_id_rot_fkey FOREIGN KEY (organization_id, project_id, service_account_id, rotated_from_id) REFERENCES identity.api_keys(organization_id, project_id, service_account_id, id);
ALTER TABLE identity.email_challenges
    ADD CONSTRAINT email_challenges_user_id_fkey FOREIGN KEY (user_id) REFERENCES identity.users(id);
ALTER TABLE identity.idempotency_records
    ADD CONSTRAINT idempotency_records_actor_id_fkey FOREIGN KEY (actor_id) REFERENCES identity.users(id);
ALTER TABLE identity.idempotency_records
    ADD CONSTRAINT idempotency_records_organization_id_fkey FOREIGN KEY (organization_id) REFERENCES identity.organizations(id);
ALTER TABLE identity.idempotency_records
    ADD CONSTRAINT idempotency_records_organization_id_resource_id_fkey FOREIGN KEY (organization_id, resource_id) REFERENCES identity.api_keys(organization_id, id);
ALTER TABLE identity.memberships
    ADD CONSTRAINT memberships_organization_id_fkey FOREIGN KEY (organization_id) REFERENCES identity.organizations(id);
ALTER TABLE identity.memberships
    ADD CONSTRAINT memberships_user_id_fkey FOREIGN KEY (user_id) REFERENCES identity.users(id);
ALTER TABLE identity.organization_invitations
    ADD CONSTRAINT organization_invitations_accepted_by_fkey FOREIGN KEY (accepted_by) REFERENCES identity.users(id);
ALTER TABLE identity.organization_invitations
    ADD CONSTRAINT organization_invitations_invited_by_fkey FOREIGN KEY (invited_by) REFERENCES identity.users(id);
ALTER TABLE identity.organization_invitations
    ADD CONSTRAINT organization_invitations_organization_id_fkey FOREIGN KEY (organization_id) REFERENCES identity.organizations(id);
ALTER TABLE identity.organizations
    ADD CONSTRAINT organizations_personal_owner_id_fkey FOREIGN KEY (personal_owner_id) REFERENCES identity.users(id) ON DELETE RESTRICT;
ALTER TABLE identity.password_setup_tokens
    ADD CONSTRAINT password_setup_tokens_user_id_fkey FOREIGN KEY (user_id) REFERENCES identity.users(id);
ALTER TABLE identity.platform_role_bindings
    ADD CONSTRAINT platform_role_bindings_user_id_fkey FOREIGN KEY (user_id) REFERENCES identity.users(id);
ALTER TABLE identity.project_grants
    ADD CONSTRAINT project_grants_organization_id_fkey FOREIGN KEY (organization_id) REFERENCES identity.organizations(id);
ALTER TABLE identity.project_grants
    ADD CONSTRAINT project_grants_organization_id_membership_id_fkey FOREIGN KEY (organization_id, membership_id) REFERENCES identity.memberships(organization_id, id);
ALTER TABLE identity.project_grants
    ADD CONSTRAINT project_grants_organization_id_project_id_fkey FOREIGN KEY (organization_id, project_id) REFERENCES identity.projects(organization_id, id);
ALTER TABLE identity.projects
    ADD CONSTRAINT projects_organization_id_fkey FOREIGN KEY (organization_id) REFERENCES identity.organizations(id);
ALTER TABLE identity.service_accounts
    ADD CONSTRAINT service_accounts_organization_id_fkey FOREIGN KEY (organization_id) REFERENCES identity.organizations(id);
ALTER TABLE identity.service_accounts
    ADD CONSTRAINT service_accounts_organization_id_project_id_fkey FOREIGN KEY (organization_id, project_id) REFERENCES identity.projects(organization_id, id);

-- catalog
ALTER TABLE catalog.deployments
    ADD CONSTRAINT deployments_model_id_fkey FOREIGN KEY (model_id) REFERENCES catalog.models(id);
ALTER TABLE catalog.deployments
    ADD CONSTRAINT deployments_upstream_id_credential_id_fkey FOREIGN KEY (upstream_id, credential_id) REFERENCES catalog.upstream_credentials(upstream_id, id);
ALTER TABLE catalog.deployments
    ADD CONSTRAINT deployments_upstream_id_fkey FOREIGN KEY (upstream_id) REFERENCES catalog.upstreams(id);
ALTER TABLE catalog.quota_policies
    ADD CONSTRAINT quota_policies_organization_id_fkey FOREIGN KEY (organization_id) REFERENCES identity.organizations(id);
ALTER TABLE catalog.route_policies
    ADD CONSTRAINT route_policies_model_id_fkey FOREIGN KEY (model_id) REFERENCES catalog.models(id);
ALTER TABLE catalog.route_policies
    ADD CONSTRAINT route_policies_organization_id_fkey FOREIGN KEY (organization_id) REFERENCES identity.organizations(id);
ALTER TABLE catalog.upstream_credentials
    ADD CONSTRAINT upstream_credentials_upstream_id_fkey FOREIGN KEY (upstream_id) REFERENCES catalog.upstreams(id);

-- billing
ALTER TABLE billing.account_restrictions
    ADD CONSTRAINT account_restrictions_account_id_fkey FOREIGN KEY (account_id) REFERENCES billing.accounts(id);
ALTER TABLE billing.adjustments
    ADD CONSTRAINT adjustments_organization_id_account_id_currency_fkey FOREIGN KEY (organization_id, account_id, currency) REFERENCES billing.accounts(organization_id, id, currency);
ALTER TABLE billing.adjustments
    ADD CONSTRAINT adjustments_organization_id_account_id_source_entry_id_fkey FOREIGN KEY (organization_id, account_id, source_entry_id) REFERENCES billing.ledger_entries(organization_id, account_id, id);
ALTER TABLE billing.adjustments
    ADD CONSTRAINT adjustments_source_entry_id_fkey FOREIGN KEY (source_entry_id) REFERENCES billing.ledger_entries(id);
ALTER TABLE billing.budget_holds
    ADD CONSTRAINT budget_holds_organization_id_period_id_fkey FOREIGN KEY (organization_id, period_id) REFERENCES billing.budget_periods(organization_id, id);
ALTER TABLE billing.budget_holds
    ADD CONSTRAINT budget_holds_organization_id_request_id_fkey FOREIGN KEY (organization_id, request_id) REFERENCES billing.request_runs(organization_id, id);
ALTER TABLE billing.budget_periods
    ADD CONSTRAINT budget_periods_organization_id_policy_id_fkey FOREIGN KEY (organization_id, policy_id) REFERENCES billing.budget_policies(organization_id, id);
ALTER TABLE billing.budget_policies
    ADD CONSTRAINT budget_policies_organization_id_api_key_id_fkey FOREIGN KEY (organization_id, api_key_id) REFERENCES identity.api_keys(organization_id, id);
ALTER TABLE billing.budget_policies
    ADD CONSTRAINT budget_policies_organization_id_fkey FOREIGN KEY (organization_id) REFERENCES billing.accounts(organization_id);
ALTER TABLE billing.budget_policies
    ADD CONSTRAINT budget_policies_organization_id_project_id_fkey FOREIGN KEY (organization_id, project_id) REFERENCES identity.projects(organization_id, id);
ALTER TABLE billing.container_extensions
    ADD CONSTRAINT container_extensions_meter_fkey FOREIGN KEY (meter) REFERENCES billing.container_sessions(id);
ALTER TABLE billing.container_extensions
    ADD CONSTRAINT container_extensions_request_id_fkey FOREIGN KEY (request_id) REFERENCES billing.request_runs(id);
ALTER TABLE billing.container_operations
    ADD CONSTRAINT container_operations_meter_fkey FOREIGN KEY (meter) REFERENCES billing.container_sessions(id);
ALTER TABLE billing.container_sessions
    ADD CONSTRAINT container_sessions_cost_price_id_fkey FOREIGN KEY (cost_price_id) REFERENCES billing.price_versions(id);
ALTER TABLE billing.cost_corrections
    ADD CONSTRAINT cost_corrections_attempt_id_fkey FOREIGN KEY (attempt_id) REFERENCES billing.request_attempts(id);
ALTER TABLE billing.cost_corrections
    ADD CONSTRAINT cost_corrections_cost_price_version_id_fkey FOREIGN KEY (cost_price_version_id) REFERENCES billing.price_versions(id);
ALTER TABLE billing.cost_corrections
    ADD CONSTRAINT cost_decision_reference FOREIGN KEY (decision_id) REFERENCES billing.case_decisions(id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE billing.execution_usage_receipts
    ADD CONSTRAINT execution_usage_receipts_request_id_fkey FOREIGN KEY (request_id) REFERENCES billing.request_runs(id);
ALTER TABLE billing.funding_records
    ADD CONSTRAINT funding_receipt_scope FOREIGN KEY (organization_id, account_id, payment_order_id, payment_receipt_id) REFERENCES billing.payment_receipts(organization_id, account_id, order_id, id);
ALTER TABLE billing.funding_records
    ADD CONSTRAINT funding_records_organization_id_account_id_currency_fkey FOREIGN KEY (organization_id, account_id, currency) REFERENCES billing.accounts(organization_id, id, currency);
ALTER TABLE billing.funding_records
    ADD CONSTRAINT funding_records_receiving_account_id_fkey FOREIGN KEY (receiving_account_id) REFERENCES billing.receiving_accounts(id);
ALTER TABLE billing.guardrail_usage_receipts
    ADD CONSTRAINT guardrail_usage_receipts_attempt_id_fkey FOREIGN KEY (attempt_id) REFERENCES billing.request_attempts(id);
ALTER TABLE billing.ledger_entries
    ADD CONSTRAINT ledger_entries_organization_id_account_id_currency_fkey FOREIGN KEY (organization_id, account_id, currency) REFERENCES billing.accounts(organization_id, id, currency);
ALTER TABLE billing.ledger_entries
    ADD CONSTRAINT ledger_entries_organization_id_account_id_reverses_entry_i_fkey FOREIGN KEY (organization_id, account_id, reverses_entry_id) REFERENCES billing.ledger_entries(organization_id, account_id, id);
ALTER TABLE billing.ledger_entries
    ADD CONSTRAINT ledger_entries_reverses_entry_id_fkey FOREIGN KEY (reverses_entry_id) REFERENCES billing.ledger_entries(id);
ALTER TABLE billing.normalized_usage
    ADD CONSTRAINT normalized_usage_attempt_id_fkey FOREIGN KEY (attempt_id) REFERENCES billing.request_attempts(id);
ALTER TABLE billing.normalized_usage
    ADD CONSTRAINT normalized_usage_event_id_fkey FOREIGN KEY (event_id) REFERENCES billing.usage_events(id);
ALTER TABLE billing.payment_cases
    ADD CONSTRAINT payment_cases_order_id_fkey FOREIGN KEY (order_id) REFERENCES billing.payment_orders(id);
ALTER TABLE billing.payment_channel_versions
    ADD CONSTRAINT payment_channel_versions_channel_id_fkey FOREIGN KEY (channel_id) REFERENCES billing.payment_channels(id);
ALTER TABLE billing.payment_channels
    ADD CONSTRAINT payment_channel_active_version FOREIGN KEY (id, active_config_id) REFERENCES billing.payment_channel_versions(channel_id, id) DEFERRABLE INITIALLY DEFERRED;
ALTER TABLE billing.payment_events
    ADD CONSTRAINT payment_events_channel_id_verification_version_id_fkey FOREIGN KEY (channel_id, verification_version_id) REFERENCES billing.payment_channel_versions(channel_id, id);
ALTER TABLE billing.payment_events
    ADD CONSTRAINT payment_events_order_id_fkey FOREIGN KEY (order_id) REFERENCES billing.payment_orders(id);
ALTER TABLE billing.payment_events
    ADD CONSTRAINT payment_events_order_id_operation_id_fkey FOREIGN KEY (order_id, operation_id) REFERENCES billing.payment_operations(order_id, id);
ALTER TABLE billing.payment_observations
    ADD CONSTRAINT payment_observations_config_version_id_fkey FOREIGN KEY (config_version_id) REFERENCES billing.payment_channel_versions(id);
ALTER TABLE billing.payment_observations
    ADD CONSTRAINT payment_observations_order_id_fkey FOREIGN KEY (order_id) REFERENCES billing.payment_orders(id);
ALTER TABLE billing.payment_operations
    ADD CONSTRAINT payment_operations_order_id_fkey FOREIGN KEY (order_id) REFERENCES billing.payment_orders(id);
ALTER TABLE billing.payment_operations
    ADD CONSTRAINT payment_retry_observation FOREIGN KEY (order_id, retry_observation_id) REFERENCES billing.payment_observations(order_id, id);
ALTER TABLE billing.payment_orders
    ADD CONSTRAINT order_funding_scope FOREIGN KEY (organization_id, account_id, funding_record_id) REFERENCES billing.funding_records(organization_id, account_id, id);
ALTER TABLE billing.payment_orders
    ADD CONSTRAINT order_ledger_scope FOREIGN KEY (organization_id, account_id, ledger_entry_id) REFERENCES billing.ledger_entries(organization_id, account_id, id);
ALTER TABLE billing.payment_orders
    ADD CONSTRAINT payment_orders_channel_id_config_version_id_fkey FOREIGN KEY (channel_id, config_version_id) REFERENCES billing.payment_channel_versions(channel_id, id);
ALTER TABLE billing.payment_orders
    ADD CONSTRAINT payment_orders_id_verified_observation_id_fkey FOREIGN KEY (id, verified_observation_id) REFERENCES billing.payment_observations(order_id, id);
ALTER TABLE billing.payment_orders
    ADD CONSTRAINT payment_orders_organization_id_account_id_currency_fkey FOREIGN KEY (organization_id, account_id, currency) REFERENCES billing.accounts(organization_id, id, currency);
ALTER TABLE billing.payment_receipts
    ADD CONSTRAINT payment_receipts_observation_id_fkey FOREIGN KEY (observation_id) REFERENCES billing.payment_observations(id);
ALTER TABLE billing.payment_receipts
    ADD CONSTRAINT receipt_collection_scope FOREIGN KEY (organization_id, account_id, order_id, currency) REFERENCES billing.payment_orders(organization_id, account_id, id, collection_currency);
ALTER TABLE billing.report_changes
    ADD CONSTRAINT report_changes_request_id_fkey FOREIGN KEY (request_id) REFERENCES billing.request_runs(id);
ALTER TABLE billing.request_attempts
    ADD CONSTRAINT request_attempts_cost_price_version_id_fkey FOREIGN KEY (cost_price_version_id) REFERENCES billing.price_versions(id);
ALTER TABLE billing.request_attempts
    ADD CONSTRAINT request_attempts_request_id_fkey FOREIGN KEY (request_id) REFERENCES billing.request_runs(id);
ALTER TABLE billing.request_runs
    ADD CONSTRAINT gateway_request_snapshot FOREIGN KEY (cluster_id, config_revision) REFERENCES publication.config_snapshots(cluster_id, revision);
ALTER TABLE billing.request_runs
    ADD CONSTRAINT request_runs_cluster_id_node_id_fkey FOREIGN KEY (cluster_id, node_id) REFERENCES publication.gateway_principals(cluster_id, node_id);
ALTER TABLE billing.request_runs
    ADD CONSTRAINT request_runs_discount_version_id_fkey FOREIGN KEY (discount_version_id) REFERENCES billing.discount_versions(id);
ALTER TABLE billing.request_runs
    ADD CONSTRAINT request_runs_node_id_fkey FOREIGN KEY (node_id) REFERENCES publication.gateway_principals(node_id);
ALTER TABLE billing.request_runs
    ADD CONSTRAINT request_runs_organization_id_account_id_fkey FOREIGN KEY (organization_id, account_id) REFERENCES billing.accounts(organization_id, id);
ALTER TABLE billing.request_runs
    ADD CONSTRAINT request_runs_organization_id_project_id_service_account_id_fkey FOREIGN KEY (organization_id, project_id, service_account_id, api_key_id) REFERENCES identity.api_keys(organization_id, project_id, service_account_id, id);
ALTER TABLE billing.request_runs
    ADD CONSTRAINT request_runs_review_decision_id_fkey FOREIGN KEY (review_decision_id) REFERENCES billing.case_decisions(id);
ALTER TABLE billing.request_runs
    ADD CONSTRAINT request_runs_sale_price_version_id_fkey FOREIGN KEY (sale_price_version_id) REFERENCES billing.price_versions(id);
ALTER TABLE billing.review_cases
    ADD CONSTRAINT review_cases_request_id_fkey FOREIGN KEY (request_id) REFERENCES billing.request_runs(id);
ALTER TABLE billing.upstream_statements
    ADD CONSTRAINT upstream_statements_attempt_id_fkey FOREIGN KEY (attempt_id) REFERENCES billing.request_attempts(id);
ALTER TABLE billing.usage_events
    ADD CONSTRAINT usage_events_attempt_id_fkey FOREIGN KEY (attempt_id) REFERENCES billing.request_attempts(id);
ALTER TABLE billing.usage_events
    ADD CONSTRAINT usage_events_request_id_fkey FOREIGN KEY (request_id) REFERENCES billing.request_runs(id);
ALTER TABLE billing.usage_jobs
    ADD CONSTRAINT usage_jobs_request_id_fkey FOREIGN KEY (request_id) REFERENCES billing.request_runs(id);
ALTER TABLE billing.vector_storage_contracts
    ADD CONSTRAINT vector_storage_contracts_cluster_id_revision_fkey FOREIGN KEY (cluster_id, revision) REFERENCES publication.config_snapshots(cluster_id, revision);
ALTER TABLE billing.vector_storage_contracts
    ADD CONSTRAINT vector_storage_contracts_cost_id_fkey FOREIGN KEY (cost_id) REFERENCES billing.price_versions(id);
ALTER TABLE billing.vector_storage_contracts
    ADD CONSTRAINT vector_storage_contracts_discount_id_fkey FOREIGN KEY (discount_id) REFERENCES billing.discount_versions(id);
ALTER TABLE billing.vector_storage_contracts
    ADD CONSTRAINT vector_storage_contracts_hold_id_fkey FOREIGN KEY (hold_id) REFERENCES billing.request_runs(id);
ALTER TABLE billing.vector_storage_contracts
    ADD CONSTRAINT vector_storage_contracts_sale_id_fkey FOREIGN KEY (sale_id) REFERENCES billing.price_versions(id);
ALTER TABLE billing.vector_storage_days
    ADD CONSTRAINT vector_storage_days_store_id_fkey FOREIGN KEY (store_id) REFERENCES billing.vector_storage_contracts(id);
ALTER TABLE billing.vector_storage_segments
    ADD CONSTRAINT vector_storage_segments_day_id_fkey FOREIGN KEY (day_id) REFERENCES billing.vector_storage_days(id);

-- publication
ALTER TABLE publication.batch_items
    ADD CONSTRAINT batch_items_batch_id_fkey FOREIGN KEY (batch_id) REFERENCES publication.message_batches(id);
ALTER TABLE publication.cluster_automation
    ADD CONSTRAINT cluster_automation_cluster_id_fkey FOREIGN KEY (cluster_id) REFERENCES publication.gateway_clusters(id);
ALTER TABLE publication.cluster_keys
    ADD CONSTRAINT cluster_keys_cluster_id_fkey FOREIGN KEY (cluster_id) REFERENCES publication.gateway_clusters(id);
ALTER TABLE publication.cluster_policies
    ADD CONSTRAINT cluster_policies_cluster_id_fkey FOREIGN KEY (cluster_id) REFERENCES publication.gateway_clusters(id);
ALTER TABLE publication.config_outbox
    ADD CONSTRAINT config_outbox_cluster_id_revision_fkey FOREIGN KEY (cluster_id, revision) REFERENCES publication.config_snapshots(cluster_id, revision);
ALTER TABLE publication.config_snapshots
    ADD CONSTRAINT config_snapshots_cluster_id_fkey FOREIGN KEY (cluster_id) REFERENCES publication.gateway_clusters(id);
ALTER TABLE publication.container_files
    ADD CONSTRAINT container_files_container_id_fkey FOREIGN KEY (container_id) REFERENCES publication.execution_containers(id);
ALTER TABLE publication.credential_envelopes
    ADD CONSTRAINT credential_envelopes_cluster_id_revision_fkey FOREIGN KEY (cluster_id, revision) REFERENCES publication.config_snapshots(cluster_id, revision);
ALTER TABLE publication.gateway_principals
    ADD CONSTRAINT gateway_principals_cluster_id_fkey FOREIGN KEY (cluster_id) REFERENCES publication.gateway_clusters(id);
ALTER TABLE publication.node_config_acks
    ADD CONSTRAINT node_config_acks_cluster_id_revision_fkey FOREIGN KEY (cluster_id, revision) REFERENCES publication.config_snapshots(cluster_id, revision);
ALTER TABLE publication.node_config_acks
    ADD CONSTRAINT node_config_acks_node_id_fkey FOREIGN KEY (node_id) REFERENCES publication.gateway_principals(node_id);
ALTER TABLE publication.previous_keys
    ADD CONSTRAINT previous_keys_cluster_id_fkey FOREIGN KEY (cluster_id) REFERENCES publication.gateway_clusters(id);
ALTER TABLE publication.purge_request_items
    ADD CONSTRAINT purge_request_items_purge_id_fkey FOREIGN KEY (purge_id) REFERENCES publication.upstream_purges(id);
ALTER TABLE publication.purge_request_items
    ADD CONSTRAINT purge_request_items_request_id_fkey FOREIGN KEY (request_id) REFERENCES publication.purge_requests(id);
ALTER TABLE publication.release_commands
    ADD CONSTRAINT release_commands_cluster_id_revision_fkey FOREIGN KEY (cluster_id, revision) REFERENCES publication.config_snapshots(cluster_id, revision);
ALTER TABLE publication.snapshot_index
    ADD CONSTRAINT snapshot_index_cluster_id_revision_fkey FOREIGN KEY (cluster_id, revision) REFERENCES publication.config_snapshots(cluster_id, revision);
ALTER TABLE publication.stored_responses
    ADD CONSTRAINT stored_responses_cluster_id_fkey FOREIGN KEY (cluster_id) REFERENCES publication.gateway_clusters(id);
ALTER TABLE publication.vector_file_batches
    ADD CONSTRAINT vector_file_batches_store_id_fkey FOREIGN KEY (store_id) REFERENCES publication.vector_stores(id);
