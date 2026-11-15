import json
import os
from pathlib import Path
from uuid import uuid4

import httpx
import psycopg
import pytest
from mail_stub import PASSWORD, install_mail

from nexus.api import create_app
from nexus.config import MigrationSettings, RuntimeSettings


@pytest.fixture(scope="session")
def service_directory():
    return Path(os.environ["NEXUS_TEST_SERVICES"])


@pytest.fixture
def runtime_settings(service_directory):
    return RuntimeSettings(_env_file=service_directory / ".env.api", environment="test")


@pytest.fixture
def migration_settings(service_directory):
    return MigrationSettings(_env_file=service_directory / ".env.migrate")


@pytest.fixture
def admin(service_directory):
    data = json.loads((service_directory / "services.json").read_text())
    if not data["database"].startswith("nexus_test_"):
        pytest.fail("integration reset is restricted to the isolated test database")
    with psycopg.connect(
        host="127.0.0.1",
        port=data["pg_port"],
        dbname=data["database"],
        user="nexus_local_admin",
        password=data["admin_password"],
        autocommit=True,
    ) as connection:
        yield connection


@pytest.fixture(autouse=True)
def reset_identity(admin):
    admin.execute(
        "TRUNCATE reporting.export_jobs,reporting.request_facts,reporting.audit_events, "
        "billing.upstream_statements,billing.ingest_counters,"
        "billing.account_restrictions,billing.report_changes,billing.review_cases,"
        "billing.account_checks,billing.case_decisions, "
        "billing.cost_corrections,publication.audit_events,publication.limit_namespaces, "
        "publication.container_operations,billing.container_operations,billing.container_extensions,"
        "billing.container_sessions, "
        "billing.guardrail_usage_receipts, billing.execution_usage_receipts, "
        "publication.container_files, "
        "publication.execution_containers, "
        "billing.vector_storage_segments,billing.vector_storage_days, "
        "billing.vector_storage_contracts, "
        "publication.vector_file_batches,publication.vector_stores, "
        "publication.conversations, publication.batch_items, publication.message_batches, "
        "publication.purge_request_items, publication.purge_requests, "
        "publication.upstream_purges, publication.inactive_keys, "
        "publication.response_jobs, publication.tenant_files, "
        "publication.stored_responses, publication.previous_keys, publication.gateway_principals, "
        "publication.cluster_automation, publication.gateway_clusters, "
        "publication.node_config_acks, "
        "publication.config_outbox, publication.config_snapshots, "
        "publication.credential_envelopes, publication.release_commands, "
        "catalog.route_policies, catalog.quota_policies, billing.usage_jobs, "
        "billing.normalized_usage, billing.usage_events, "
        "billing.payment_cases, billing.payment_receipts, billing.payment_observations, "
        "billing.payment_events, billing.payment_rate_buckets, billing.payment_operations, "
        "billing.payment_orders, "
        "billing.payment_channel_versions, billing.payment_channels, billing.request_attempts, "
        "billing.budget_holds, billing.request_runs, "
        "billing.budget_periods, billing.budget_policies, billing.price_versions, "
        "billing.discount_versions, billing.audit_events, billing.idempotency_records, "
        "billing.adjustments, billing.ledger_entries, billing.funding_records, "
        "billing.receiving_accounts, billing.outbox, billing.inbox, billing.accounts, "
        "billing.settings, identity.inbox, catalog.audit_events, catalog.deployments, "
        "catalog.upstream_credentials, "
        "catalog.upstreams, catalog.models, identity.access_policy_models, "
        "identity.access_policies, "
        "identity.idempotency_records, identity.api_keys, identity.service_accounts, "
        "identity.project_grants, identity.projects, identity.outbox, "
        "identity.email_challenges, identity.organization_invitations, "
        "identity.memberships, identity.organizations, "
        "identity.admin_sessions, identity.password_setup_tokens, identity.audit_events, "
        "identity.platform_role_bindings, identity.users"
    )

    admin.execute(
        "UPDATE identity.auth_policy SET registration=false,email_verification=false,"
        "turnstile=false,version=1"
    )
    admin.execute(
        "INSERT INTO billing.discount_versions (id,factor,state,effective_at,published_at) "
        "VALUES ('00000000-0000-0000-0000-000000000001',1,'published', "
        "'1970-01-01T00:00:00Z',clock_timestamp())"
    )


@pytest.fixture
async def http(runtime_settings):
    settings = runtime_settings.model_copy(update={"redis_namespace": "nexus:test:" + uuid4().hex})
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        await app.state.identity_service.bootstrap_admin(
            "root-admin", "integration-password-with-entropy"
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as client:
            response = await client.post(
                "/api/v1/auth/sessions",
                json={"login": "root-admin", "password": "integration-password-with-entropy"},
            )
            assert response.status_code == 201, response.text
            yield client, response.json()["access_token"], app, settings


@pytest.fixture
def delivery(http):
    client, root, app, settings = http
    mail = app.state.identity_service
    messages, challenges = install_mail(mail, settings)
    return client, root, mail, messages, challenges


@pytest.fixture
def signup(delivery, admin):
    """Open verified self-service registration and complete it for one login."""
    client, _, mail, messages, _ = delivery
    admin.execute(
        "UPDATE identity.auth_policy SET registration=true,email_verification=true,"
        "version=version+1"
    )

    async def register(login, email=None):
        email = email or login.lower() + "@example.com"
        started = await client.post(
            "/api/v1/auth/registrations",
            json={"login": login, "password": PASSWORD, "email": email},
        )
        assert started.status_code == 202, started.text
        assert await mail.dispatch_email() == 1
        token = messages.pop().token
        created = await client.post(
            "/api/v1/auth/registrations/verify", json={"token": token, "password": PASSWORD}
        )
        assert created.status_code == 201, created.text
        assert created.headers["cache-control"] == "no-store"
        return created.json()

    return register
