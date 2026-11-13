import json
import os
from pathlib import Path
from uuid import uuid4

import httpx
import psycopg
import pytest

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
        "TRUNCATE identity.idempotency_records, identity.api_keys, identity.service_accounts, "
        "identity.project_grants, identity.projects, identity.outbox, "
        "identity.memberships, identity.organizations, "
        "identity.admin_sessions, identity.password_setup_tokens, identity.audit_events, "
        "identity.platform_role_bindings, identity.users"
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
