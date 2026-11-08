"""Module schemas and the minimal identity storage needed by bootstrap."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001_m1_foundation"
down_revision = None
branch_labels = None
depends_on = None

MODULES = ("identity", "catalog", "billing", "publication", "reporting")


def upgrade() -> None:
    # Function EXECUTE is a global default; per-schema REVOKE cannot remove it.
    op.execute("ALTER DEFAULT PRIVILEGES REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC")
    for module in MODULES:
        op.execute(f'CREATE SCHEMA "{module}"')
        op.execute(f'REVOKE ALL ON SCHEMA "{module}" FROM PUBLIC')
        op.execute(f'GRANT USAGE ON SCHEMA "{module}" TO "nexus_{module}"')
    op.execute("CREATE SCHEMA gateway_v1")
    op.execute("REVOKE ALL ON SCHEMA gateway_v1 FROM PUBLIC")
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("login", sa.String(254), nullable=False, unique=True),
        sa.Column("password_hash", sa.String(512), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "login = lower(btrim(login)) AND length(login) >= 3", name="normalized_login"
        ),
        sa.CheckConstraint("status IN ('active', 'disabled')", name="user_status"),
        schema="identity",
    )
    op.create_table(
        "platform_role_bindings",
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("identity.users.id"), primary_key=True),
        sa.Column("role", sa.String(32), primary_key=True),
        sa.CheckConstraint("role IN ('platform_admin', 'platform_finance')", name="platform_role"),
        schema="identity",
    )
    op.create_table(
        "audit_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("actor_type", sa.String(20), nullable=False),
        sa.Column("action", sa.String(100), nullable=False),
        sa.Column("resource_id", sa.Uuid(), nullable=False),
        sa.Column("details", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        schema="identity",
    )
    op.create_index(
        "audit_events_created_at_idx", "audit_events", ["created_at", "id"], schema="identity"
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON identity.users, "
        "identity.platform_role_bindings TO nexus_identity"
    )
    op.execute("GRANT SELECT, INSERT ON identity.audit_events TO nexus_identity")


def downgrade() -> None:
    op.drop_table("audit_events", schema="identity")
    op.drop_table("platform_role_bindings", schema="identity")
    op.drop_table("users", schema="identity")
    op.execute("DROP SCHEMA gateway_v1")
    for module in reversed(MODULES):
        op.execute(f'DROP SCHEMA "{module}"')
