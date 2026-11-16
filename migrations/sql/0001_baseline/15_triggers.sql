-- Triggers: revision counters and their notifications, immutability guards, personal organizations
-- and the settlement and report bookkeeping.

-- identity
CREATE TRIGGER announce_revision AFTER UPDATE ON identity.source_revision FOR EACH STATEMENT EXECUTE FUNCTION identity.announce_revision();
CREATE TRIGGER personal_membership_binding BEFORE DELETE OR UPDATE ON identity.memberships FOR EACH ROW EXECUTE FUNCTION identity.protect_personal_membership();
CREATE TRIGGER personal_organization_binding BEFORE DELETE OR UPDATE OF personal_owner_id ON identity.organizations FOR EACH ROW EXECUTE FUNCTION identity.protect_personal_organization();
CREATE TRIGGER source_revision AFTER INSERT OR DELETE OR UPDATE ON identity.access_policies FOR EACH STATEMENT EXECUTE FUNCTION identity.advance_revision();
CREATE TRIGGER source_revision AFTER INSERT OR DELETE OR UPDATE ON identity.access_policy_models FOR EACH STATEMENT EXECUTE FUNCTION identity.advance_revision();
CREATE TRIGGER source_revision AFTER INSERT OR DELETE OR UPDATE ON identity.api_keys FOR EACH STATEMENT EXECUTE FUNCTION identity.advance_revision();
CREATE TRIGGER source_revision AFTER INSERT OR DELETE OR UPDATE ON identity.organizations FOR EACH STATEMENT EXECUTE FUNCTION identity.advance_revision();
CREATE TRIGGER source_revision AFTER INSERT OR DELETE OR UPDATE ON identity.projects FOR EACH STATEMENT EXECUTE FUNCTION identity.advance_revision();
CREATE TRIGGER source_revision AFTER INSERT OR DELETE OR UPDATE ON identity.service_accounts FOR EACH STATEMENT EXECUTE FUNCTION identity.advance_revision();
CREATE TRIGGER user_personal_organization AFTER INSERT ON identity.users FOR EACH ROW EXECUTE FUNCTION identity.on_user_created();

-- catalog
CREATE TRIGGER announce_revision AFTER UPDATE ON catalog.source_revision FOR EACH STATEMENT EXECUTE FUNCTION catalog.announce_revision();
CREATE TRIGGER source_revision AFTER INSERT OR DELETE OR UPDATE ON catalog.deployments FOR EACH STATEMENT EXECUTE FUNCTION catalog.advance_revision();
CREATE TRIGGER source_revision AFTER INSERT OR DELETE OR UPDATE ON catalog.models FOR EACH STATEMENT EXECUTE FUNCTION catalog.advance_revision();
CREATE TRIGGER source_revision AFTER INSERT OR DELETE OR UPDATE ON catalog.quota_policies FOR EACH STATEMENT EXECUTE FUNCTION catalog.advance_revision();
CREATE TRIGGER source_revision AFTER INSERT OR DELETE OR UPDATE ON catalog.resource_quota_defaults FOR EACH STATEMENT EXECUTE FUNCTION catalog.advance_revision();
CREATE TRIGGER source_revision AFTER INSERT OR DELETE OR UPDATE ON catalog.route_policies FOR EACH STATEMENT EXECUTE FUNCTION catalog.advance_revision();
CREATE TRIGGER source_revision AFTER INSERT OR DELETE OR UPDATE ON catalog.upstream_credentials FOR EACH STATEMENT EXECUTE FUNCTION catalog.advance_revision();
CREATE TRIGGER source_revision AFTER INSERT OR DELETE OR UPDATE ON catalog.upstreams FOR EACH STATEMENT EXECUTE FUNCTION catalog.advance_revision();

-- billing
CREATE TRIGGER account_check AFTER INSERT ON billing.accounts FOR EACH ROW EXECUTE FUNCTION billing.track_account_check();
CREATE TRIGGER account_identity BEFORE UPDATE ON billing.accounts FOR EACH ROW EXECUTE FUNCTION billing.protect_account_identity();
CREATE TRIGGER adjustments_posted BEFORE DELETE OR UPDATE ON billing.adjustments FOR EACH ROW EXECUTE FUNCTION billing.protect_posted_record();
CREATE TRIGGER announce_revision AFTER UPDATE ON billing.source_revision FOR EACH STATEMENT EXECUTE FUNCTION billing.announce_revision();
CREATE TRIGGER attempt_immutable BEFORE UPDATE ON billing.request_attempts FOR EACH ROW EXECUTE FUNCTION billing.protect_attempt();
CREATE TRIGGER attempt_report AFTER INSERT OR UPDATE ON billing.request_attempts FOR EACH ROW EXECUTE FUNCTION billing.mark_report_change();
CREATE TRIGGER batch_attempt BEFORE INSERT ON billing.request_attempts FOR EACH ROW WHEN ((new.sequence IS DISTINCT FROM 1)) EXECUTE FUNCTION billing.protect_batch_attempt();
CREATE TRIGGER container_settled AFTER UPDATE OF reservation_state ON billing.request_runs FOR EACH ROW WHEN ((((new.billing_policy_version)::text = 'openai_container_v1'::text) AND ((new.reservation_state)::text <> 'held'::text))) EXECUTE FUNCTION billing.v1_container_settled();
CREATE TRIGGER corrected_cost_report AFTER INSERT ON billing.cost_corrections FOR EACH ROW EXECUTE FUNCTION billing.dirty_corrected_cost();
CREATE TRIGGER default_payment_quote BEFORE INSERT ON billing.payment_orders FOR EACH ROW EXECUTE FUNCTION billing.default_payment_quote();
CREATE TRIGGER discount_versions_immutable BEFORE DELETE OR UPDATE ON billing.discount_versions FOR EACH ROW EXECUTE FUNCTION billing.protect_published_version();
CREATE TRIGGER fact_report AFTER INSERT ON billing.usage_events FOR EACH ROW EXECUTE FUNCTION billing.mark_report_change();
CREATE TRIGGER funding_records_posted BEFORE DELETE OR UPDATE ON billing.funding_records FOR EACH ROW EXECUTE FUNCTION billing.protect_posted_record();
CREATE TRIGGER gateway_request_origin BEFORE UPDATE OF cluster_id, node_id, config_revision ON billing.request_runs FOR EACH ROW EXECUTE FUNCTION billing.protect_gateway_binding();
CREATE TRIGGER immutable_container_limits BEFORE UPDATE OF container_unit, container_minutes_limit, container_tariff ON billing.request_runs FOR EACH ROW WHEN ((old.container_unit, old.container_minutes_limit, old.container_tariff) IS DISTINCT FROM (new.container_unit, new.container_minutes_limit, new.container_tariff)) EXECUTE FUNCTION billing.protect_search_limit();
CREATE TRIGGER immutable_execution_bounds BEFORE UPDATE OF execution_mode, execution_seconds_limit ON billing.request_runs FOR EACH ROW WHEN (((old.execution_mode IS DISTINCT FROM new.execution_mode) OR (old.execution_seconds_limit IS DISTINCT FROM new.execution_seconds_limit))) EXECUTE FUNCTION billing.protect_search_limit();
CREATE TRIGGER immutable_guardrail_limit BEFORE UPDATE OF guardrail_unit_limit ON billing.request_runs FOR EACH ROW WHEN ((old.guardrail_unit_limit <> new.guardrail_unit_limit)) EXECUTE FUNCTION billing.protect_search_limit();
CREATE TRIGGER immutable_usage_fact BEFORE DELETE OR UPDATE ON billing.usage_events FOR EACH ROW EXECUTE FUNCTION billing.reject_immutable_change();
CREATE TRIGGER immutable_vector_bounds BEFORE UPDATE OF file_search_limit, storage_byte_seconds ON billing.request_runs FOR EACH ROW WHEN (((old.file_search_limit IS DISTINCT FROM new.file_search_limit) OR (old.storage_byte_seconds IS DISTINCT FROM new.storage_byte_seconds))) EXECUTE FUNCTION billing.protect_search_limit();
CREATE TRIGGER ledger_immutable BEFORE DELETE OR UPDATE ON billing.ledger_entries FOR EACH ROW EXECUTE FUNCTION billing.reject_immutable_change();
CREATE TRIGGER new_review_evidence AFTER INSERT ON billing.usage_events FOR EACH ROW EXECUTE FUNCTION billing.bump_review_evidence();
CREATE TRIGGER payment_channel_binding BEFORE UPDATE ON billing.payment_channels FOR EACH ROW EXECUTE FUNCTION billing.protect_payment_channel();
CREATE TRIGGER payment_config_immutable BEFORE UPDATE ON billing.payment_channel_versions FOR EACH ROW EXECUTE FUNCTION billing.protect_payment_config();
CREATE TRIGGER payment_event_immutable BEFORE UPDATE ON billing.payment_events FOR EACH ROW EXECUTE FUNCTION billing.protect_payment_event();
CREATE TRIGGER payment_event_no_delete BEFORE DELETE ON billing.payment_events FOR EACH ROW EXECUTE FUNCTION billing.reject_immutable_change();
CREATE TRIGGER payment_observations_immutable BEFORE DELETE OR UPDATE ON billing.payment_observations FOR EACH ROW EXECUTE FUNCTION billing.reject_immutable_change();
CREATE TRIGGER payment_operation_intent BEFORE UPDATE ON billing.payment_operations FOR EACH ROW EXECUTE FUNCTION billing.protect_payment_operation();
CREATE TRIGGER payment_order_immutable BEFORE UPDATE ON billing.payment_orders FOR EACH ROW EXECUTE FUNCTION billing.protect_payment_order();
CREATE TRIGGER payment_quote_immutable BEFORE UPDATE OF collection_amount, collection_currency, exchange_rate ON billing.payment_orders FOR EACH ROW WHEN ((old.collection_amount, old.collection_currency, old.exchange_rate) IS DISTINCT FROM (new.collection_amount, new.collection_currency, new.exchange_rate)) EXECUTE FUNCTION billing.reject_immutable_change();
CREATE TRIGGER payment_receipts_immutable BEFORE DELETE OR UPDATE ON billing.payment_receipts FOR EACH ROW EXECUTE FUNCTION billing.reject_immutable_change();
CREATE TRIGGER price_versions_immutable BEFORE DELETE OR UPDATE ON billing.price_versions FOR EACH ROW EXECUTE FUNCTION billing.protect_published_version();
CREATE TRIGGER protect_search_limit BEFORE UPDATE OF web_search_limit ON billing.request_runs FOR EACH ROW WHEN ((old.web_search_limit <> new.web_search_limit)) EXECUTE FUNCTION billing.protect_search_limit();
CREATE TRIGGER receiving_identity BEFORE UPDATE ON billing.receiving_accounts FOR EACH ROW EXECUTE FUNCTION billing.protect_receiving_identity();
CREATE TRIGGER request_admission BEFORE UPDATE ON billing.request_runs FOR EACH ROW EXECUTE FUNCTION billing.protect_admission();
CREATE TRIGGER request_report AFTER INSERT OR UPDATE ON billing.request_runs FOR EACH ROW EXECUTE FUNCTION billing.mark_report_change();
CREATE TRIGGER request_review AFTER INSERT OR UPDATE OF state, review_reason, reservation_state ON billing.request_runs FOR EACH ROW EXECUTE FUNCTION billing.track_request_review();
CREATE TRIGGER resolved_attempt_review AFTER UPDATE OF state ON billing.request_attempts FOR EACH ROW WHEN (((new.state)::text = 'confirmed'::text)) EXECUTE FUNCTION billing.close_cost_review();
CREATE TRIGGER settlement_backpressure BEFORE INSERT ON billing.request_runs FOR EACH ROW WHEN ((new.node_id IS NOT NULL)) EXECUTE FUNCTION billing.check_settlement_backlog();
CREATE TRIGGER source_revision AFTER INSERT OR DELETE OR UPDATE ON billing.discount_versions FOR EACH STATEMENT EXECUTE FUNCTION billing.advance_revision();
CREATE TRIGGER source_revision AFTER INSERT OR DELETE OR UPDATE ON billing.price_versions FOR EACH STATEMENT EXECUTE FUNCTION billing.advance_revision();

-- publication
CREATE TRIGGER announce_cluster AFTER INSERT OR UPDATE OF version ON publication.gateway_clusters FOR EACH STATEMENT EXECUTE FUNCTION publication.announce_cluster();
CREATE TRIGGER announce_policy AFTER INSERT OR UPDATE ON publication.cluster_policies FOR EACH STATEMENT EXECUTE FUNCTION publication.announce_cluster();
CREATE TRIGGER announce_policy AFTER INSERT OR UPDATE ON publication.cluster_policy_defaults FOR EACH STATEMENT EXECUTE FUNCTION publication.announce_cluster();
