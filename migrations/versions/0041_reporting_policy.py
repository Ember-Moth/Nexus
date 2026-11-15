"""The export policy in ``reporting.export_policy``: rate, retention and size of exports.

``managed_sha256`` and ``managed_at`` name the policy file that locked the row through
``nexus settings import``.
"""

from alembic import op

revision = "0041_reporting_policy"
down_revision = "0040_billing_policy"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE reporting.export_policy (
        singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton),
        per_org_minute integer NOT NULL DEFAULT 10 CHECK(per_org_minute BETWEEN 1 AND 1000),
        ttl_seconds integer NOT NULL DEFAULT 86400 CHECK(ttl_seconds BETWEEN 60 AND 2592000),
        max_rows integer NOT NULL DEFAULT 10000 CHECK(max_rows BETWEEN 1 AND 100000),
        version integer NOT NULL DEFAULT 1 CHECK(version > 0),
        managed_sha256 text CHECK(managed_sha256 ~ '^[0-9a-f]{64}$'),
        managed_at timestamptz,
        CHECK((managed_sha256 IS NULL) = (managed_at IS NULL))
    )""")
    op.execute("INSERT INTO reporting.export_policy DEFAULT VALUES")
    op.execute("GRANT SELECT, UPDATE ON reporting.export_policy TO nexus_reporting")


def downgrade():
    op.execute("DROP TABLE reporting.export_policy")
