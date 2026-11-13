from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config

pytestmark = pytest.mark.integration


def test_migration_round_trip_and_repeat_upgrade(migration_settings, admin):
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    config.attributes["settings"] = migration_settings
    try:
        command.downgrade(config, "base")
        assert admin.execute("SELECT to_regclass('identity.users')").fetchone()[0] is None
        assert admin.execute("SELECT count(*) FROM nexus_meta.alembic_version").fetchone()[0] == 0
    finally:
        command.upgrade(config, "head")
    command.upgrade(config, "head")
    assert admin.execute("SELECT version_num FROM nexus_meta.alembic_version").fetchall() == [
        ("0004_catalog_and_access",)
    ]


def test_runtime_cannot_migrate_and_functions_are_not_public(admin):
    for role in ("nexus_api", "nexus_worker"):
        assert admin.execute(
            "SELECT rolsuper, rolcreaterole, rolcreatedb, rolinherit "
            "FROM pg_roles WHERE rolname = %s",
            (role,),
        ).fetchone() == (False, False, False, False)
        assert (
            admin.execute("SELECT pg_has_role(%s, 'nexus_migrator', 'MEMBER')", (role,)).fetchone()[
                0
            ]
            is False
        )
    admin.execute("SET ROLE nexus_api")
    try:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            admin.execute("CREATE SCHEMA forbidden_runtime_schema")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            admin.execute("UPDATE nexus_meta.alembic_version SET version_num = 'wrong'")
    finally:
        admin.execute("RESET ROLE")
    admin.execute("SET ROLE nexus_migrator")
    try:
        admin.execute(
            "CREATE FUNCTION billing.test_private_function() RETURNS integer "
            "LANGUAGE SQL AS 'SELECT 1'"
        )
        assert (
            admin.execute(
                "SELECT has_function_privilege('nexus_billing', "
                "'billing.test_private_function()', 'EXECUTE')"
            ).fetchone()[0]
            is False
        )
    finally:
        admin.execute("DROP FUNCTION IF EXISTS billing.test_private_function()")
        admin.execute("RESET ROLE")


def test_m2_upgrade_preserves_m1_administrator(migration_settings, admin):
    from uuid import uuid4

    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    config.attributes["settings"] = migration_settings
    user_id = uuid4()
    try:
        command.downgrade(config, "0001_m1_foundation")
        admin.execute(
            "INSERT INTO identity.users (id, login, password_hash) VALUES (%s,%s,%s)",
            (user_id, "existing-admin", "existing-test-hash"),
        )
        admin.execute(
            "INSERT INTO identity.platform_role_bindings (user_id,role) "
            "VALUES (%s,'platform_admin')",
            (user_id,),
        )
    finally:
        command.upgrade(config, "head")
    assert admin.execute(
        "SELECT login,password_hash,status FROM identity.users WHERE id=%s",
        (user_id,),
    ).fetchone() == ("existing-admin", "existing-test-hash", "active")
    assert (
        admin.execute(
            "SELECT role FROM identity.platform_role_bindings WHERE user_id=%s", (user_id,)
        ).fetchone()[0]
        == "platform_admin"
    )
