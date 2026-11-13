"""Management sessions, password setup, organizations and memberships."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0002_identity_access"
down_revision = "0001_m1_foundation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("user_status", "users", schema="identity")
    op.alter_column("users", "password_hash", nullable=True, schema="identity")
    op.create_check_constraint(
        "user_status", "users", "status IN ('pending', 'active', 'disabled')", schema="identity"
    )
    op.create_check_constraint(
        "active_password",
        "users",
        "status <> 'active' OR password_hash IS NOT NULL",
        schema="identity",
    )
    for name in ("admin_sessions", "password_setup_tokens"):
        op.create_table(
            name,
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column("user_id", sa.Uuid(), sa.ForeignKey("identity.users.id"), nullable=False),
            sa.Column("token_digest", sa.String(64), nullable=False, unique=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("revoked_at", sa.DateTime(timezone=True)),
            sa.CheckConstraint("expires_at > created_at", name=f"{name}_expiry"),
            schema="identity",
        )
        op.create_index(f"{name}_user_idx", name, ["user_id"], schema="identity")
    op.create_table(
        "organizations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="provisioning"),
        sa.Column("resume_status", sa.String(20)),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "status IN ('provisioning', 'active', 'suspended', 'archived')",
            name="organization_status",
        ),
        sa.CheckConstraint(
            "resume_status IS NULL OR resume_status IN ('provisioning', 'active')",
            name="organization_resume_status",
        ),
        sa.CheckConstraint("version > 0 AND length(btrim(name)) > 0", name="organization_fields"),
        schema="identity",
    )
    op.create_table(
        "memberships",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "organization_id", sa.Uuid(), sa.ForeignKey("identity.organizations.id"), nullable=False
        ),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("identity.users.id"), nullable=False),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("organization_id", "user_id", name="membership_user_unique"),
        sa.UniqueConstraint("organization_id", "id", name="membership_tenant_unique"),
        sa.CheckConstraint(
            "role IN ('org_owner', 'org_developer', 'org_finance')", name="membership_role"
        ),
        sa.CheckConstraint(
            "status IN ('active', 'disabled') AND version > 0", name="membership_status"
        ),
        schema="identity",
    )
    op.create_index(
        "memberships_user_idx", "memberships", ["user_id", "organization_id"], schema="identity"
    )
    op.create_table(
        "outbox",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("topic", sa.String(100), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("aggregate_id", sa.Uuid(), nullable=False),
        sa.Column("aggregate_version", sa.Integer(), nullable=False),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint(
            "topic", "aggregate_id", "aggregate_version", name="identity_outbox_fact"
        ),
        schema="identity",
    )
    op.add_column("audit_events", sa.Column("actor_id", sa.Uuid()), schema="identity")
    op.add_column("audit_events", sa.Column("organization_id", sa.Uuid()), schema="identity")
    for name in ("users", "organizations", "memberships"):
        op.create_index(f"{name}_page_idx", name, ["created_at", "id"], schema="identity")
    op.execute(
        "GRANT SELECT, INSERT, UPDATE ON identity.admin_sessions, "
        "identity.password_setup_tokens, identity.organizations, "
        "identity.memberships, identity.outbox TO nexus_identity"
    )


def downgrade() -> None:
    # Pending users have never had a usable password. Preserve them as disabled on rollback.
    op.execute(
        "UPDATE identity.users SET status='disabled', password_hash='!disabled' "
        "WHERE password_hash IS NULL"
    )
    for name in ("users", "organizations", "memberships"):
        op.drop_index(f"{name}_page_idx", table_name=name, schema="identity")
    op.drop_column("audit_events", "organization_id", schema="identity")
    op.drop_column("audit_events", "actor_id", schema="identity")
    for name in (
        "outbox",
        "memberships",
        "organizations",
        "password_setup_tokens",
        "admin_sessions",
    ):
        op.drop_table(name, schema="identity")
    op.drop_constraint("active_password", "users", schema="identity")
    op.drop_constraint("user_status", "users", schema="identity")
    op.create_check_constraint(
        "user_status", "users", "status IN ('active', 'disabled')", schema="identity"
    )
    op.alter_column("users", "password_hash", nullable=False, schema="identity")
