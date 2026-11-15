from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config

from nexus.shared.storage import SCHEMA_REVISION

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
        (SCHEMA_REVISION,)
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


def test_migrations_fail_fast_instead_of_queueing_behind_a_busy_table(
    migration_settings, admin, service_directory
):
    import json
    import threading
    from time import monotonic

    from nexus.shared.storage import SCHEMA_REVISION

    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    config.attributes["settings"] = migration_settings.model_copy(
        update={"migration_lock_timeout_ms": 300}
    )
    # 0030 alters billing.request_attempts, which Relay writes through start_attempt.
    command.downgrade(config, "0029_guardrail_units")
    data = json.loads((service_directory / "services.json").read_text())
    outcome = {}

    def upgrade():
        started = monotonic()
        try:
            command.upgrade(config, "head")
            outcome["error"] = None
        except Exception as error:
            outcome["error"] = error
        outcome["elapsed"] = monotonic() - started

    try:
        with psycopg.connect(
            host="127.0.0.1",
            port=data["pg_port"],
            dbname=data["database"],
            user="nexus_local_admin",
            password=data["admin_password"],
        ) as holder:
            # A long transaction holds the table. Without lock_timeout the ALTER would wait for it
            # and, while waiting, block every later reader and writer of the table.
            holder.execute("SELECT count(*) FROM billing.request_attempts")
            worker = threading.Thread(target=upgrade)
            worker.start()
            worker.join(30)
            stuck = worker.is_alive()
            holder.rollback()
            worker.join()
        assert not stuck, "the migration queued behind the lock holder"
        assert isinstance(outcome["error"], Exception)
        assert "lock timeout" in str(outcome["error"])
        assert outcome["elapsed"] < 10
        # The failed revision rolled back alone; nothing after it ran.
        assert admin.execute("SELECT version_num FROM nexus_meta.alembic_version").fetchall() == [
            ("0029_guardrail_units",)
        ]
    finally:
        command.upgrade(config, "head")
    assert admin.execute("SELECT version_num FROM nexus_meta.alembic_version").fetchall() == [
        (SCHEMA_REVISION,)
    ]


def columns(admin, schema, table):
    return {
        row[0]
        for row in admin.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema=%s AND table_name=%s",
            (schema, table),
        )
    }


def resource_usage_signatures(admin):
    return {
        row[0]
        for row in admin.execute(
            "SELECT n.nspname||'.'||pg_get_function_identity_arguments(p.oid) FROM pg_proc p "
            "JOIN pg_namespace n ON n.oid=p.pronamespace "
            "WHERE p.proname IN ('resource_usage','v1_resource_usage')"
        )
    }


def test_policy_tables_start_at_their_defaults_and_downgrade(migration_settings, admin):
    """0039-0043 create typed policy rows with defaults; downgrades restore 0038's shape."""
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    config.attributes["settings"] = migration_settings
    org = admin.execute(
        "INSERT INTO identity.organizations(id,name,status) "
        "VALUES (gen_random_uuid(),'Migrated','active') RETURNING id"
    ).fetchone()[0]
    try:
        admin.execute("UPDATE identity.auth_policy SET login_per_ip=7")
        admin.execute(
            "INSERT INTO catalog.quota_policies(id,organization_id,layer,scope_type,scope_id,"
            "metric,limit_value) VALUES (gen_random_uuid(),%s,'platform','organization',%s,"
            "'resources_per_key',3),(gen_random_uuid(),%s,'platform','organization',%s,'rpm',9)",
            (org, org, org, org),
        )
        command.downgrade(config, "0038_upstream_resources")
        assert "login_per_ip" not in columns(admin, "identity", "auth_policy")
        assert "version" in columns(admin, "identity", "auth_policy")
        assert columns(admin, "billing", "settings") >= {"id", "currency"}
        assert "version" not in columns(admin, "billing", "settings")
        for table in (
            "reporting.export_policy",
            "catalog.resource_quota_defaults",
            "publication.cluster_policies",
            "publication.cluster_policy_defaults",
        ):
            assert admin.execute("SELECT to_regclass(%s)", (table,)).fetchone()[0] is None
        # Resource count overrides cannot exist under the previous metric check.
        assert admin.execute("SELECT metric FROM catalog.quota_policies").fetchall() == [("rpm",)]
        assert resource_usage_signatures(admin) == {
            "gateway_v1.kind text, kid uuid, rev bigint, keys uuid[]",
            "publication.kind text, kid uuid, keys uuid[]",
        }
    finally:
        command.upgrade(config, "head")
    assert admin.execute(
        "SELECT login_window_seconds,login_per_account,login_per_ip,mail_window_seconds,"
        "mail_per_recipient,mail_per_recipient_total,mail_per_account,mail_per_ip,mail_global,"
        "mail_queue_ttl_seconds,session_ttl_seconds,password_setup_ttl_seconds,"
        "platform_email_recovery,'admin'=ANY(reserved_logins),cardinality(reserved_logins),"
        "managed_sha256 FROM identity.auth_policy"
    ).fetchone() == (
        60,
        10,
        60,
        3600,
        3,
        10,
        10,
        20,
        1000,
        3600,
        28800,
        86400,
        False,
        True,
        17,
        None,
    )
    assert admin.execute(
        "SELECT currency,vector_storage_prepaid_days,vector_storage_lapse_policy,version "
        "FROM billing.settings"
    ).fetchall() == [("USD", 7, "delete", 1)]
    assert admin.execute(
        "SELECT per_org_minute,ttl_seconds,max_rows,version FROM reporting.export_policy"
    ).fetchone() == (10, 86400, 10000, 1)
    assert admin.execute(
        "SELECT resources_per_key,resources_per_organization,vector_stores_per_key,"
        "vector_stores_per_organization FROM catalog.resource_quota_defaults"
    ).fetchone() == (10000, 100000, 100, 1000)
    assert admin.execute(
        "SELECT max_attempts,failover_budget_ms,purge_grace_seconds,stream_lifetime_seconds,"
        "auto_publish_quiet_seconds,auto_publish_max_delay_seconds FROM "
        "publication.cluster_policy_defaults"
    ).fetchone() == (2, 10000, 604800, 1800, 3.0, 30.0)
    # The bounded counts replace the unbounded ones.
    assert resource_usage_signatures(admin) == {
        "gateway_v1.kind text, kid uuid, rev bigint, keys uuid[], key_cap bigint, org_cap bigint",
        "publication.kind text, kid uuid, keys uuid[], key_cap bigint, org_cap bigint",
    }
    for statement in (
        "UPDATE identity.auth_policy SET login_per_ip=0",
        "UPDATE identity.auth_policy SET mail_per_recipient=11,mail_per_recipient_total=10",
        "UPDATE billing.settings SET currency='usd'",
        "UPDATE reporting.export_policy SET max_rows=100001",
        "UPDATE catalog.resource_quota_defaults SET vector_stores_per_key=0",
        "UPDATE publication.cluster_policy_defaults SET max_attempts=5",
        "UPDATE publication.cluster_policy_defaults SET auto_publish_max_delay_seconds=1",
        "UPDATE reporting.export_policy SET managed_sha256='abc'",
    ):
        with pytest.raises(psycopg.errors.CheckViolation):
            admin.execute(statement)
    admin.execute("DELETE FROM catalog.quota_policies")
    admin.execute("DELETE FROM identity.organizations WHERE id=%s", (org,))
