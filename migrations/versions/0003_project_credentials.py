"""Project authorization and application credential lifecycle."""

import sqlalchemy as sa
from alembic import op

revision = "0003_project_credentials"
down_revision = "0002_identity_access"
branch_labels = None
depends_on = None


def scoped_columns():
    return [
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "organization_id", sa.Uuid(), sa.ForeignKey("identity.organizations.id"), nullable=False
        ),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    ]


def upgrade():
    op.create_table(
        "projects",
        *scoped_columns(),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.UniqueConstraint("organization_id", "id", name="project_tenant_unique"),
        sa.UniqueConstraint("organization_id", "name", name="project_name_unique"),
        sa.CheckConstraint(
            "status IN ('active','disabled','archived') AND version > 0 "
            "AND length(btrim(name)) > 0",
            name="project_fields",
        ),
        schema="identity",
    )
    op.create_table(
        "project_grants",
        *scoped_columns(),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("membership_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.ForeignKeyConstraint(
            ["organization_id", "project_id"],
            ["identity.projects.organization_id", "identity.projects.id"],
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "membership_id"],
            ["identity.memberships.organization_id", "identity.memberships.id"],
        ),
        sa.UniqueConstraint(
            "organization_id", "project_id", "membership_id", name="project_grant_unique"
        ),
        sa.CheckConstraint(
            "status IN ('active','revoked') AND version > 0", name="project_grant_fields"
        ),
        schema="identity",
    )
    op.create_table(
        "service_accounts",
        *scoped_columns(),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.ForeignKeyConstraint(
            ["organization_id", "project_id"],
            ["identity.projects.organization_id", "identity.projects.id"],
        ),
        sa.UniqueConstraint(
            "organization_id", "project_id", "id", name="service_account_scope_unique"
        ),
        sa.UniqueConstraint(
            "organization_id", "project_id", "name", name="service_account_name_unique"
        ),
        sa.CheckConstraint(
            "status IN ('active','disabled') AND version > 0 AND length(btrim(name)) > 0",
            name="service_account_fields",
        ),
        schema="identity",
    )
    op.create_table(
        "api_keys",
        *scoped_columns(),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("service_account_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("digest", sa.String(64), nullable=False, unique=True),
        sa.Column("prefix", sa.String(16), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("rotated_at", sa.DateTime(timezone=True)),
        sa.Column("rotated_from_id", sa.Uuid(), unique=True),
        sa.ForeignKeyConstraint(
            ["organization_id", "project_id", "service_account_id"],
            [
                "identity.service_accounts.organization_id",
                "identity.service_accounts.project_id",
                "identity.service_accounts.id",
            ],
        ),
        sa.UniqueConstraint("organization_id", "id", name="api_key_tenant_unique"),
        sa.UniqueConstraint(
            "organization_id",
            "project_id",
            "service_account_id",
            "id",
            name="api_key_rotation_scope_unique",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "project_id", "service_account_id", "rotated_from_id"],
            [
                "identity.api_keys.organization_id",
                "identity.api_keys.project_id",
                "identity.api_keys.service_account_id",
                "identity.api_keys.id",
            ],
        ),
        sa.CheckConstraint("version > 0 AND length(btrim(name)) > 0", name="api_key_fields"),
        schema="identity",
    )
    op.create_table(
        "idempotency_records",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "organization_id", sa.Uuid(), sa.ForeignKey("identity.organizations.id"), nullable=False
        ),
        sa.Column("actor_id", sa.Uuid(), sa.ForeignKey("identity.users.id"), nullable=False),
        sa.Column("operation", sa.String(40), nullable=False),
        sa.Column("key_digest", sa.String(64), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("resource_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "resource_id"],
            ["identity.api_keys.organization_id", "identity.api_keys.id"],
        ),
        sa.UniqueConstraint(
            "organization_id",
            "actor_id",
            "operation",
            "key_digest",
            name="identity_idempotency_unique",
        ),
        schema="identity",
    )
    for name in ("projects", "project_grants", "service_accounts", "api_keys"):
        op.create_index(
            f"{name}_tenant_page_idx",
            name,
            ["organization_id", "created_at", "id"],
            schema="identity",
        )
    op.create_index(
        "grants_membership_idx",
        "project_grants",
        ["organization_id", "membership_id", "project_id"],
        schema="identity",
    )
    op.execute(
        "GRANT SELECT, INSERT, UPDATE ON identity.projects, identity.project_grants, "
        "identity.service_accounts, identity.api_keys TO nexus_identity"
    )
    op.execute("GRANT SELECT, INSERT ON identity.idempotency_records TO nexus_identity")


def downgrade():
    for name in (
        "idempotency_records",
        "api_keys",
        "service_accounts",
        "project_grants",
        "projects",
    ):
        op.drop_table(name, schema="identity")
