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
        ("0010_settlement_backpressure",)
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


def test_m3_upgrade_preserves_and_provisions_existing_m2_organization(
    migration_settings, runtime_settings, admin
):
    import asyncio
    from uuid import uuid4

    from nexus.bootstrap import Application

    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    config.attributes["settings"] = migration_settings
    user_id, org_id, event_id = uuid4(), uuid4(), uuid4()
    try:
        command.downgrade(config, "0004_catalog_and_access")
        admin.execute(
            "INSERT INTO identity.users(id,login,password_hash) VALUES(%s,%s,%s)",
            (user_id, "m2-preserved", "m2-password-hash"),
        )
        admin.execute(
            "INSERT INTO identity.organizations(id,name) VALUES(%s,%s)",
            (org_id, "Existing M2 organization"),
        )
        admin.execute(
            "INSERT INTO identity.outbox(id,topic,schema_version,"
            "aggregate_id,aggregate_version,payload) "
            "VALUES(%s,'identity.organization_created',1,%s,1,'{}'::jsonb)",
            (event_id, org_id),
        )
    finally:
        command.upgrade(config, "head")
    assert admin.execute(
        "SELECT password_hash,version FROM identity.users WHERE id=%s", (user_id,)
    ).fetchone() == ("m2-password-hash", 1)
    assert admin.execute(
        "SELECT status,billing_account_id FROM identity.organizations WHERE id=%s", (org_id,)
    ).fetchone() == ("provisioning", None)

    async def consume():
        application = Application(runtime_settings)
        try:
            await application.process_billing()
        finally:
            await application.close()

    asyncio.run(consume())
    assert admin.execute(
        "SELECT o.status,a.currency FROM identity.organizations o "
        "JOIN billing.accounts a ON a.id=o.billing_account_id WHERE o.id=%s",
        (org_id,),
    ).fetchone() == ("active", "USD")


def test_m3p_upgrade_keeps_existing_manual_funding(migration_settings, admin):
    from decimal import Decimal
    from uuid import uuid4

    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    config.attributes["settings"] = migration_settings
    user_id, org_id, account_id, receiving_id, funding_id, entry_id = [uuid4() for _ in range(6)]
    try:
        command.downgrade(config, "0006_pricing_reservations")
        admin.execute(
            "INSERT INTO identity.users(id,login,password_hash) VALUES(%s,%s,%s)",
            (user_id, "m3-finance-preserved", "m3-test-password-hash"),
        )
        admin.execute(
            "INSERT INTO identity.organizations(id,name,status,"
            "billing_account_id) VALUES(%s,'M3 legacy','active',%s)",
            (org_id, account_id),
        )
        admin.execute(
            "INSERT INTO billing.accounts(id,organization_id,currency,"
            "balance,entry_sequence) VALUES(%s,%s,'USD',5,1)",
            (account_id, org_id),
        )
        admin.execute(
            "INSERT INTO billing.receiving_accounts(id,name,reference,"
            "receipt_namespace,currency) VALUES(%s,'Legacy bank',"
            "'legacy-bank','manual:legacy-bank','USD')",
            (receiving_id,),
        )
        admin.execute(
            "INSERT INTO billing.funding_records(id,organization_id,"
            "account_id,receiving_account_id,receipt_namespace,"
            "external_reference,amount,currency,received_at,evidence,"
            "status,created_by,posted_by,posted_at) VALUES(%s,%s,%s,%s,"
            "'manual:legacy-bank','legacy-001',5,'USD',clock_timestamp(),"
            "'Retained test receipt','posted',%s,%s,clock_timestamp())",
            (funding_id, org_id, account_id, receiving_id, user_id, user_id),
        )
        admin.execute(
            "INSERT INTO billing.ledger_entries(id,organization_id,"
            "account_id,sequence,currency,signed_amount,balance_after,"
            "kind,source_type,source_id,actor_id) VALUES(%s,%s,%s,1,"
            "'USD',5,5,'funding_credit','funding',%s,%s)",
            (entry_id, org_id, account_id, funding_id, user_id),
        )
    finally:
        command.upgrade(config, "head")
    assert admin.execute(
        "SELECT balance,held_amount,entry_sequence FROM billing.accounts WHERE id=%s", (account_id,)
    ).fetchone() == (Decimal(5), Decimal(0), 1)
    assert admin.execute(
        "SELECT source_type,system_actor,payment_order_id,created_by,"
        "posted_by,amount,status FROM billing.funding_records WHERE id=%s",
        (funding_id,),
    ).fetchone() == ("manual", None, None, user_id, user_id, Decimal(5), "posted")
    assert admin.execute("SELECT count(*) FROM billing.ledger_entries").fetchone()[0] == 1
