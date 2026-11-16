from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config

from nexus.modules.identity.domain.setup import (
    BUILTIN_ADMIN_ID,
    DEFAULT_ADMIN_EMAIL,
    DEFAULT_ADMIN_PASSWORD_HASH,
)
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
    # A reinstall starts with the built-in administrator only and the setup wizard pending.
    assert admin.execute(
        "SELECT u.id,u.login,u.password_hash,u.status,u.verified_email,u.unverified_email,b.role "
        "FROM identity.users u JOIN identity.platform_role_bindings b ON b.user_id=u.id"
    ).fetchall() == [
        (
            BUILTIN_ADMIN_ID,
            "admin",
            DEFAULT_ADMIN_PASSWORD_HASH,
            "active",
            None,
            DEFAULT_ADMIN_EMAIL,
            "platform_admin",
        )
    ]
    assert admin.execute(
        "SELECT site_name,console_url,contact_email,setup_completed_at,version,managed_sha256 "
        "FROM identity.site_policy"
    ).fetchall() == [("Nexus", None, None, None, 1, None)]


def test_builtin_admin_migration_downgrades_and_upgrades_again(migration_settings, admin):
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    config.attributes["settings"] = migration_settings
    admin.execute("UPDATE identity.site_policy SET setup_completed_at=now(),version=2")
    try:
        command.downgrade(config, "0046_export_objects")
        assert admin.execute("SELECT to_regclass('identity.site_policy')").fetchone() == (None,)
        assert "unverified_email" not in columns(admin, "identity", "users")
        # The account stays as an ordinary platform administrator.
        assert admin.execute(
            "SELECT login FROM identity.users WHERE id=%s", (BUILTIN_ADMIN_ID,)
        ).fetchone() == ("admin",)
    finally:
        command.upgrade(config, "head")
    assert admin.execute(
        "SELECT unverified_email FROM identity.users WHERE id=%s", (BUILTIN_ADMIN_ID,)
    ).fetchone() == (DEFAULT_ADMIN_EMAIL,)
    assert admin.execute("SELECT setup_completed_at FROM identity.site_policy").fetchone() == (
        None,
    )
    for statement in (
        "UPDATE identity.site_policy SET site_name=''",
        "UPDATE identity.site_policy SET console_url='ftp://x'",
        f"UPDATE identity.users SET verified_email='a@corp.internal' WHERE id='{BUILTIN_ADMIN_ID}'",
    ):
        with pytest.raises(psycopg.errors.CheckViolation):
            admin.execute(statement)


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


def test_reference_grants_do_not_deadlock_with_analyze(
    migration_settings, admin, service_directory
):
    """0032 lets nexus_billing reference billing.request_runs, the table autovacuum analyzes most.

    ANALYZE holds the table's SHARE UPDATE EXCLUSIVE lock while it rewrites the table's pg_class
    row; the holder below does both with a GRANT. Had the revision granted before locking, the
    holder's GRANT would wait for the revision while the revision's foreign key waited for the
    holder's lock, and the deadlock detector would fail one of them.
    """
    import json
    import threading
    from time import monotonic, sleep

    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    config.attributes["settings"] = migration_settings
    command.downgrade(config, "0031_model_call_bounds")
    data = json.loads((service_directory / "services.json").read_text())
    outcome = {}

    def upgrade():
        try:
            command.upgrade(config, "0032_container_coverage")
            outcome["error"] = None
        except Exception as error:
            outcome["error"] = error

    try:
        with psycopg.connect(
            host="127.0.0.1",
            port=data["pg_port"],
            dbname=data["database"],
            user="nexus_local_admin",
            password=data["admin_password"],
            options="-c statement_timeout=10000",
        ) as holder:
            holder.execute("LOCK TABLE billing.request_runs IN SHARE UPDATE EXCLUSIVE MODE")
            worker = threading.Thread(target=upgrade)
            worker.start()
            deadline = monotonic() + 10
            while not admin.execute(
                "SELECT EXISTS(SELECT FROM pg_locks WHERE NOT granted "
                "AND relation='billing.request_runs'::regclass)"
            ).fetchone()[0]:
                assert monotonic() < deadline and worker.is_alive(), outcome
                sleep(0.02)
            # Rewrites the row the way ANALYZE does; it must not wait for the revision.
            holder.execute("GRANT TRUNCATE ON billing.request_runs TO nexus_billing")
            holder.rollback()
            worker.join(30)
        assert not worker.is_alive()
        assert outcome == {"error": None}
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
