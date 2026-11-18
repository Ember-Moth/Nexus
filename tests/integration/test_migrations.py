"""The baseline migration and the machinery later revisions rely on.

The lock tests stack a probe revision on the baseline from a temporary directory, the way a
future revision would alter a table that serves traffic.
"""

import json
import os
import threading
from pathlib import Path
from time import monotonic, sleep

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
ROOT = Path(__file__).resolve().parents[2]
SCHEMAS = ("identity", "catalog", "billing", "publication", "reporting", "gateway_v1")


def alembic_config(settings, probes=None):
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["settings"] = settings
    if probes is not None:
        locations = [str(ROOT / "migrations" / "versions"), str(probes)]
        config.set_main_option("version_locations", os.pathsep.join(locations))
    return config


def probe(directory, revision, upgrade, downgrade, imports=""):
    """A revision on top of the baseline; ``upgrade``/``downgrade`` are function bodies."""
    directory.mkdir(exist_ok=True)
    (directory / f"{revision}.py").write_text(
        f"from alembic import op\n{imports}\n"
        f"revision = {revision!r}\ndown_revision = {SCHEMA_REVISION!r}\n"
        "branch_labels = None\ndepends_on = None\n\n\n"
        f"def upgrade():\n{upgrade}\n\n\ndef downgrade():\n{downgrade}\n"
    )


def holder_connection(service_directory, **options):
    data = json.loads((service_directory / "services.json").read_text())
    return psycopg.connect(
        host="127.0.0.1",
        port=data["pg_port"],
        dbname=data["database"],
        user="nexus_local_admin",
        password=data["admin_password"],
        **options,
    )


def revisions(admin):
    return admin.execute("SELECT version_num FROM nexus_meta.alembic_version").fetchall()


def test_baseline_round_trip_and_repeat_upgrade(migration_settings, admin):
    config = alembic_config(migration_settings)
    try:
        command.downgrade(config, "base")
        assert (
            admin.execute(
                "SELECT nspname FROM pg_namespace WHERE nspname = ANY(%s)", (list(SCHEMAS),)
            ).fetchall()
            == []
        )
        assert revisions(admin) == []
        # The roles are cluster objects of provisioning and stay; so does the version table.
        assert admin.execute(
            "SELECT count(*) FROM pg_roles WHERE rolname='nexus_bridge'"
        ).fetchone() == (1,)
        assert admin.execute("SELECT count(*) FROM pg_default_acl").fetchone() == (0,)
    finally:
        command.upgrade(config, "head")
    command.upgrade(config, "head")
    assert revisions(admin) == [(SCHEMA_REVISION,)]


def test_reinstall_returns_to_the_initial_state(migration_settings, admin):
    """Whatever an installation did, base and head again leave only what the baseline writes."""
    config = alembic_config(migration_settings)
    admin.execute(
        "INSERT INTO identity.organizations(id,name,status) "
        "VALUES (gen_random_uuid(),'Reinstalled','active')"
    )
    admin.execute("UPDATE identity.auth_policy SET login_per_ip=7")
    admin.execute("UPDATE identity.site_policy SET setup_completed_at=now(),version=2")
    admin.execute("UPDATE billing.settings SET vector_storage_prepaid_days=3")
    admin.execute("UPDATE publication.cluster_policy_defaults SET max_attempts=1")
    try:
        command.downgrade(config, "base")
    finally:
        command.upgrade(config, "head")
    # The built-in administrator alone, with its personal organization, and the wizard pending.
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
        "SELECT o.name,o.status,m.role FROM identity.organizations o "
        "JOIN identity.memberships m ON m.organization_id=o.id"
    ).fetchall() == [("Personal's Organisation", "provisioning", "org_owner")]
    assert admin.execute(
        "SELECT site_name,console_url,api_url,contact_email,setup_completed_at,version,"
        "managed_sha256 FROM identity.site_policy"
    ).fetchall() == [("Nexus", None, None, None, None, 1, None)]
    # Identity mail and Turnstile start unconfigured, without any secret.
    assert admin.execute(
        "SELECT m.provider,m.credential,t.site_key,t.credential,m.version,t.version "
        "FROM identity.mail_policy m, identity.turnstile_policy t"
    ).fetchall() == [(None, None, None, None, 1, 1)]
    assert admin.execute(
        "SELECT login_window_seconds,login_per_account,login_per_ip,mail_window_seconds,"
        "mail_per_recipient,mail_per_recipient_total,mail_per_account,mail_per_ip,mail_global,"
        "mail_queue_ttl_seconds,session_ttl_seconds,password_setup_ttl_seconds,"
        "platform_email_recovery,'admin'=ANY(reserved_logins),cardinality(reserved_logins),"
        "registration,email_verification,turnstile,managed_sha256 FROM identity.auth_policy"
    ).fetchall() == [
        (60, 10, 60, 3600, 3, 10, 10, 20, 1000, 3600, 28800, 86400, False, True, 17)
        + (False, False, False, None)
    ]
    assert admin.execute(
        "SELECT currency,vector_storage_prepaid_days,vector_storage_lapse_policy,version "
        "FROM billing.settings"
    ).fetchall() == [("USD", 7, "delete", 1)]
    assert admin.execute(
        "SELECT per_org_minute,ttl_seconds,max_rows,version FROM reporting.export_policy"
    ).fetchall() == [(10, 86400, 10000, 1)]
    assert admin.execute(
        "SELECT resources_per_key,resources_per_organization,vector_stores_per_key,"
        "vector_stores_per_organization FROM catalog.resource_quota_defaults"
    ).fetchall() == [(10000, 100000, 100, 1000)]
    assert admin.execute(
        "SELECT max_attempts,failover_budget_ms,purge_grace_seconds,stream_lifetime_seconds,"
        "auto_publish_quiet_seconds,auto_publish_max_delay_seconds FROM "
        "publication.cluster_policy_defaults"
    ).fetchall() == [(2, 10000, 604800, 1800, 3.0, 30.0)]
    assert admin.execute(
        "SELECT id::text,factor,state FROM billing.discount_versions"
    ).fetchall() == [("00000000-0000-0000-0000-000000000001", 1, "published")]
    assert admin.execute(
        "SELECT (SELECT revision FROM identity.source_revision),"
        "(SELECT revision FROM catalog.source_revision),"
        "(SELECT revision FROM billing.source_revision)"
    ).fetchone() == (3, 1, 1)
    for statement in (
        "UPDATE identity.auth_policy SET login_per_ip=0",
        "UPDATE identity.auth_policy SET mail_per_recipient=11,mail_per_recipient_total=10",
        "UPDATE identity.site_policy SET site_name=''",
        "UPDATE identity.site_policy SET console_url='ftp://x'",
        "UPDATE identity.site_policy SET api_url='ftp://x'",
        "UPDATE identity.mail_policy SET provider='resend',sender='a@b.co'",
        "UPDATE identity.mail_policy SET provider='smtp',sender='a@b.co'",
        "UPDATE identity.mail_policy SET smtp_host='smtp.test'",
        "UPDATE identity.turnstile_policy SET site_key='k'",
        "UPDATE identity.turnstile_policy SET hostname='console.test'",
        f"UPDATE identity.users SET verified_email='a@corp.internal' WHERE id='{BUILTIN_ADMIN_ID}'",
        "UPDATE billing.settings SET currency='usd'",
        "UPDATE reporting.export_policy SET max_rows=100001",
        "UPDATE reporting.export_policy SET managed_sha256='abc'",
        "UPDATE catalog.resource_quota_defaults SET vector_stores_per_key=0",
        "UPDATE publication.cluster_policy_defaults SET max_attempts=5",
        "UPDATE publication.cluster_policy_defaults SET auto_publish_max_delay_seconds=1",
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
    migration_settings, admin, service_directory, tmp_path
):
    # billing.request_attempts is written by Relay through start_attempt.
    probe(
        tmp_path,
        "9001_lock_probe",
        '    op.execute("ALTER TABLE billing.request_attempts ADD COLUMN lock_probe integer")',
        '    op.execute("ALTER TABLE billing.request_attempts DROP COLUMN lock_probe")',
    )
    config = alembic_config(
        migration_settings.model_copy(update={"migration_lock_timeout_ms": 300}), tmp_path
    )
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
        with holder_connection(service_directory) as holder:
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
        # The failed revision rolled back alone.
        assert revisions(admin) == [(SCHEMA_REVISION,)]
    finally:
        if revisions(admin) != [(SCHEMA_REVISION,)]:
            command.downgrade(config, SCHEMA_REVISION)
    assert "lock_probe" not in columns(admin, "billing", "request_attempts")


def test_reference_grants_do_not_deadlock_with_analyze(
    migration_settings, admin, service_directory, tmp_path
):
    """A revision lets nexus_billing reference billing.request_runs, the table autovacuum
    analyzes most.

    ANALYZE holds the table's SHARE UPDATE EXCLUSIVE lock while it rewrites the table's pg_class
    row; the holder below does both with a GRANT. Had the revision granted before locking, the
    holder's GRANT would wait for the revision while the revision's foreign key waited for the
    holder's lock, and the deadlock detector would fail one of them.
    """
    probe(
        tmp_path,
        "9002_reference_probe",
        """    op.execute("GRANT CREATE ON SCHEMA billing TO nexus_billing")
    with references(["billing.request_runs"], "nexus_billing"):
        op.execute("SET LOCAL ROLE nexus_billing")
        op.execute(
            "CREATE TABLE billing.reference_probe(request_id uuid REFERENCES billing.request_runs)"
        )
        op.execute("RESET ROLE")
    op.execute("REVOKE CREATE ON SCHEMA billing FROM nexus_billing")""",
        '    op.execute("DROP TABLE billing.reference_probe")',
        imports="from nexus.infrastructure.migrations import references",
    )
    config = alembic_config(migration_settings, tmp_path)
    outcome = {}

    def upgrade():
        try:
            command.upgrade(config, "head")
            outcome["error"] = None
        except Exception as error:
            outcome["error"] = error

    try:
        with holder_connection(service_directory, options="-c statement_timeout=10000") as holder:
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
        assert revisions(admin) == [("9002_reference_probe",)]
        # The grant ends with the block.
        assert admin.execute(
            "SELECT has_table_privilege('nexus_billing','billing.request_runs','REFERENCES')"
        ).fetchone() == (False,)
    finally:
        command.downgrade(config, SCHEMA_REVISION)
    assert revisions(admin) == [(SCHEMA_REVISION,)]


def columns(admin, schema, table):
    return {
        row[0]
        for row in admin.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema=%s AND table_name=%s",
            (schema, table),
        ).fetchall()
    }
