"""Preserve the originating API request ID on audit events and email jobs."""

from alembic import op

revision = "0017_request_correlation"
down_revision = "0016_auth_email"
branch_labels = None
depends_on = None

TABLES = [
    f"{module}.audit_events"
    for module in ("billing", "catalog", "identity", "publication", "reporting")
]
TABLES.append("identity.email_challenges")


def upgrade():
    for table in TABLES:
        op.execute(f"ALTER TABLE {table} ADD COLUMN trace_id uuid")
        op.execute(
            f"ALTER TABLE {table} ALTER COLUMN trace_id SET DEFAULT "
            "nullif(current_setting('nexus.request_id', true), '')::uuid"
        )

        if table.endswith(".audit_events"):
            op.execute(
                f"CREATE INDEX audit_trace_idx ON {table}(trace_id) WHERE trace_id IS NOT NULL"
            )


def downgrade():
    for table in TABLES:
        op.execute(f"ALTER TABLE {table} DROP COLUMN trace_id")
