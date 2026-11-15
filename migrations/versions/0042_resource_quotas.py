"""Tenant resource count limits: platform defaults plus per-organization quota policies.

- ``catalog.resource_quota_defaults`` holds the platform defaults of the live files,
  conversations, containers, batches and vector stores a key or an organization may hold in one
  cluster. Its changes advance the catalog source revision, so automatic publication delivers
  them. ``managed_sha256`` and ``managed_at`` name the policy file that locked it.
- ``catalog.quota_policies`` accepts the four counts as metrics. They override a default for one
  organization and only exist as platform-layer, organization-scope policies.
- ``catalog.audit_events.actor_id`` becomes nullable for policy file imports, which name their
  source in ``details.actor``.
"""

from alembic import op

revision = "0042_resource_quotas"
down_revision = "0041_reporting_policy"
branch_labels = None
depends_on = None

COUNTS = ("resources_per_key", "resources_per_organization")
COUNTS += ("vector_stores_per_key", "vector_stores_per_organization")
DEFAULTS = {
    "resources_per_key": 10000,
    "resources_per_organization": 100000,
    "vector_stores_per_key": 100,
    "vector_stores_per_organization": 1000,
}
RATES = "'rpm','tpm','concurrency'"


def upgrade():
    columns = ",".join(
        f"{name} integer NOT NULL DEFAULT {DEFAULTS[name]} CHECK({name} BETWEEN 1 AND 2000000000)"
        for name in COUNTS
    )
    op.execute(f"""CREATE TABLE catalog.resource_quota_defaults (
        singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton), {columns},
        version integer NOT NULL DEFAULT 1 CHECK(version > 0),
        managed_sha256 text CHECK(managed_sha256 ~ '^[0-9a-f]{{64}}$'),
        managed_at timestamptz,
        CHECK((managed_sha256 IS NULL) = (managed_at IS NULL)))""")
    op.execute("INSERT INTO catalog.resource_quota_defaults DEFAULT VALUES")
    op.execute("GRANT SELECT, UPDATE ON catalog.resource_quota_defaults TO nexus_catalog")
    op.execute(
        "CREATE TRIGGER source_revision AFTER INSERT OR UPDATE OR DELETE "
        "ON catalog.resource_quota_defaults "
        "FOR EACH STATEMENT EXECUTE FUNCTION catalog.advance_revision()"
    )
    counts = ",".join(f"'{name}'" for name in COUNTS)
    op.execute(f"""ALTER TABLE catalog.quota_policies
        DROP CONSTRAINT quota_policies_metric_check,
        ADD CONSTRAINT quota_policies_metric_check CHECK(metric IN ({RATES},{counts})),
        ADD CONSTRAINT quota_policies_count_scope CHECK(metric IN ({RATES})
            OR (layer='platform' AND scope_type='organization' AND scope_id=organization_id))""")
    op.execute("ALTER TABLE catalog.audit_events ALTER COLUMN actor_id DROP NOT NULL")


def downgrade():
    # Import audit records keep their null actor.
    counts = ",".join(f"'{name}'" for name in COUNTS)
    op.execute(f"DELETE FROM catalog.quota_policies WHERE metric IN ({counts})")
    op.execute(f"""ALTER TABLE catalog.quota_policies
        DROP CONSTRAINT quota_policies_count_scope,
        DROP CONSTRAINT quota_policies_metric_check,
        ADD CONSTRAINT quota_policies_metric_check CHECK(metric IN ({RATES}))""")
    op.execute("DROP TABLE catalog.resource_quota_defaults")
