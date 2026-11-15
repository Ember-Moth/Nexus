"""Deployment provisioning: `nexus provision-database` and relay node logins (deploy/helm)."""

import json
import secrets
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from nexus.commands import provision_database
from nexus.config import ProvisionSettings
from nexus.infrastructure.provisioning import (
    GROUP_ROLES,
    ProvisioningError,
    ensure_gateway_login,
)

pytestmark = pytest.mark.integration


def admin_url(service_directory, database="postgres"):
    data = json.loads((service_directory / "services.json").read_text())
    return (
        f"postgresql://nexus_local_admin:{data['admin_password']}"
        f"@127.0.0.1:{data['pg_port']}/{database}"
    ), data["database"]


def test_provision_database_is_idempotent_and_keeps_existing_roles(service_directory, admin):
    url, database = admin_url(service_directory)
    before = admin.execute(
        "SELECT rolname, rolpassword IS NULL FROM pg_authid WHERE rolname LIKE 'nexus_%' ORDER BY 1"
    ).fetchall()
    # Roles already exist (as with CloudNativePG managed roles): no passwords are needed.
    for _ in range(2):
        provision_database(ProvisionSettings(database_url=url, database=database))
    after = admin.execute(
        "SELECT rolname, rolpassword IS NULL FROM pg_authid WHERE rolname LIKE 'nexus_%' ORDER BY 1"
    ).fetchall()
    assert before == after
    for role in GROUP_ROLES:
        assert admin.execute(
            "SELECT NOT rolcanlogin FROM pg_roles WHERE rolname=%s", (role,)
        ).fetchone() == (True,)
    assert admin.execute(
        "SELECT has_database_privilege('nexus_migrator', current_database(), 'CREATE'), "
        "has_database_privilege('nexus_api', current_database(), 'CREATE'), "
        "pg_has_role('nexus_api', 'nexus_billing', 'MEMBER'), "
        "pg_has_role('nexus_api', 'nexus_bridge', 'MEMBER')"
    ).fetchone() == (True, False, True, False)


def test_provisioning_needs_a_password_for_a_missing_login(service_directory):
    url, _ = admin_url(service_directory)
    settings = ProvisionSettings(database_url=url, database="nexus_absent_" + uuid4().hex[:8])
    with psycopg.connect(url, autocommit=True) as connection:
        connection.execute("ALTER ROLE nexus_worker RENAME TO nexus_worker_saved")
        try:
            with pytest.raises(ProvisioningError, match="nexus_worker is missing"):
                provision_database(settings)
        finally:
            connection.execute("ALTER ROLE nexus_worker_saved RENAME TO nexus_worker")


def test_gateway_logins_are_plain_members_of_nexus_gateway(admin):
    role = "nexus_relay_test_" + uuid4().hex[:8]
    first, second = secrets.token_urlsafe(16), secrets.token_urlsafe(16)
    try:
        assert ensure_gateway_login(admin, role, first) is True
        info = admin.info

        def login(password):
            return psycopg.connect(
                host=info.host, port=info.port, dbname=info.dbname, user=role, password=password
            )

        with login(first) as node:
            query = "SELECT pg_has_role(current_user, 'nexus_gateway', 'USAGE')"
            assert node.execute(query).fetchone()[0]
        # The Secret is authoritative: a second run re-keys the login.
        assert ensure_gateway_login(admin, role, second) is False
        with pytest.raises(psycopg.OperationalError):
            login(first).close()
        login(second).close()
        admin.execute(sql.SQL("GRANT nexus_billing TO {}").format(sql.Identifier(role)))
        with pytest.raises(ProvisioningError, match="not a plain gateway node login"):
            ensure_gateway_login(admin, role, second)
        with pytest.raises(ProvisioningError):
            ensure_gateway_login(admin, "postgres", second)
    finally:
        admin.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))
