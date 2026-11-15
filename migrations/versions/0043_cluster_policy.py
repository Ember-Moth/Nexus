"""Relay and automatic publication policy: platform defaults and per-cluster overrides.

- ``publication.cluster_policy_defaults`` is the single row every cluster follows: attempts per
  request, failover budget, purge grace, resumed stream lifetime, and the auto-publisher's quiet
  window and maximum delay. ``managed_sha256`` and ``managed_at`` name the policy file that
  locked it through ``nexus settings import``.
- ``publication.cluster_policies`` has one row per cluster; a non-null column overrides the
  default for that cluster.
- Changes to either wake the auto-publisher like any other cluster change.
- ``gateway_v1.resource_usage`` takes the per-key and per-organization limits from the node's
  snapshot and counts no further than them; it replaces the unbounded variant of 0034.
"""

from pathlib import Path

from alembic import op

revision = "0043_cluster_policy"
down_revision = "0042_resource_quotas"
branch_labels = None
depends_on = None
SQL = Path(__file__).resolve().parents[1] / "sql" / revision
# (schema, owner, grantee, file, new signature, replaced signature)
CONTRACTS = [
    (
        "publication",
        "nexus_publication",
        "nexus_bridge",
        "usage.sql",
        "v1_resource_usage(text,uuid,uuid[],bigint,bigint)",
        "v1_resource_usage(text,uuid,uuid[])",
    ),
    (
        "gateway_v1",
        "nexus_bridge",
        "nexus_gateway",
        "gateway.sql",
        "resource_usage(text,uuid,bigint,uuid[],bigint,bigint)",
        "resource_usage(text,uuid,bigint,uuid[])",
    ),
]
# (column, type, default, check)
POLICY = (
    ("max_attempts", "integer", "2", "BETWEEN 1 AND 4"),
    ("failover_budget_ms", "integer", "10000", "BETWEEN 100 AND 60000"),
    ("purge_grace_seconds", "integer", "604800", ">= 0"),
    ("stream_lifetime_seconds", "integer", "1800", ">= 1"),
    ("auto_publish_quiet_seconds", "double precision", "3", "BETWEEN 0 AND 300"),
    ("auto_publish_max_delay_seconds", "double precision", "30", "> 0 AND {c} <= 3600"),
)


def checked(column, check):
    return f"CHECK({column} {check.format(c=column)})"


def run(schema, owner, grantee, path, signature):
    op.execute(f"GRANT CREATE ON SCHEMA {schema} TO {owner}")
    op.execute(f"SET LOCAL ROLE {owner}")
    op.get_bind().exec_driver_sql(path.read_text(), execution_options={"no_parameters": True})
    op.execute(f"REVOKE ALL ON FUNCTION {schema}.{signature} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {schema}.{signature} TO {grantee}")
    op.execute("RESET ROLE")
    op.execute(f"REVOKE CREATE ON SCHEMA {schema} FROM {owner}")


def upgrade():
    defaults = ",".join(
        f"{c} {kind} NOT NULL DEFAULT {value} {checked(c, check)}"
        for c, kind, value, check in POLICY
    )
    op.execute(f"""CREATE TABLE publication.cluster_policy_defaults (
        singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton), {defaults},
        version integer NOT NULL DEFAULT 1 CHECK(version > 0),
        managed_sha256 text CHECK(managed_sha256 ~ '^[0-9a-f]{{64}}$'),
        managed_at timestamptz,
        CHECK((managed_sha256 IS NULL) = (managed_at IS NULL)),
        CHECK(auto_publish_max_delay_seconds >= auto_publish_quiet_seconds))""")
    op.execute("INSERT INTO publication.cluster_policy_defaults DEFAULT VALUES")
    overrides = ",".join(f"{c} {kind} {checked(c, check)}" for c, kind, _, check in POLICY)
    op.execute(f"""CREATE TABLE publication.cluster_policies (
        cluster_id uuid PRIMARY KEY REFERENCES publication.gateway_clusters, {overrides},
        version integer NOT NULL DEFAULT 1 CHECK(version > 0))""")
    op.execute(
        "INSERT INTO publication.cluster_policies(cluster_id) "
        "SELECT id FROM publication.gateway_clusters"
    )
    for table in ("cluster_policy_defaults", "cluster_policies"):
        op.execute(f"GRANT SELECT,INSERT,UPDATE ON publication.{table} TO nexus_publication")
        op.execute(
            f"CREATE TRIGGER announce_policy AFTER INSERT OR UPDATE ON publication.{table} "
            "FOR EACH STATEMENT EXECUTE FUNCTION publication.announce_cluster()"
        )
    for schema, owner, *_, replaced in CONTRACTS:
        op.execute(f"SET LOCAL ROLE {owner}")
        op.execute(f"DROP FUNCTION {schema}.{replaced}")
        op.execute("RESET ROLE")
    for schema, owner, grantee, file, signature, _ in CONTRACTS:
        run(schema, owner, grantee, SQL / file, signature)


def downgrade():
    for schema, owner, _, _, signature, _ in CONTRACTS:
        op.execute(f"SET LOCAL ROLE {owner}")
        op.execute(f"DROP FUNCTION {schema}.{signature}")
        op.execute("RESET ROLE")
    # Restore the unbounded counts of 0034 (its index on message_batches is still in place).
    for schema, owner, grantee, file, _, replaced in CONTRACTS:
        run(schema, owner, grantee, SQL / f"downgrade_{file}", replaced)
    op.execute("DROP TABLE publication.cluster_policies, publication.cluster_policy_defaults")
