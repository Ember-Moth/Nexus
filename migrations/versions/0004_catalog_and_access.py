"""Model/upstream catalog and identity-owned model access policies."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0004_catalog_and_access"
down_revision = "0003_project_credentials"
branch_labels = None
depends_on = None


def resource_columns():
    return [
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    ]


def upgrade():
    op.create_table(
        "models",
        *resource_columns(),
        sa.Column("public_name", sa.String(200), nullable=False, unique=True),
        sa.Column("display_name", sa.String(200), nullable=False),
        sa.Column("protocols", JSONB(), nullable=False),
        sa.Column("capabilities", JSONB(), nullable=False),
        sa.Column("usage_schema", sa.String(32), nullable=False),
        sa.Column("token_dimensions", JSONB(), nullable=False),
        sa.Column("max_input_tokens", sa.Integer(), nullable=False),
        sa.Column("max_output_tokens", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.CheckConstraint(
            "status IN ('active','disabled','archived') AND version > 0", name="model_status"
        ),
        sa.CheckConstraint("max_input_tokens > 0 AND max_output_tokens > 0", name="model_limits"),
        schema="catalog",
    )
    op.create_table(
        "upstreams",
        *resource_columns(),
        sa.Column("name", sa.String(200), nullable=False, unique=True),
        sa.Column("base_url", sa.String(2048), nullable=False),
        sa.Column("protocol_family", sa.String(32), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.CheckConstraint(
            "status IN ('active','disabled','archived') AND version > 0", name="upstream_status"
        ),
        schema="catalog",
    )
    op.create_table(
        "upstream_credentials",
        *resource_columns(),
        sa.Column("upstream_id", sa.Uuid(), sa.ForeignKey("catalog.upstreams.id"), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("key_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("upstream_id", "id", name="credential_upstream_unique"),
        sa.CheckConstraint(
            "status IN ('active','revoked') AND version > 0 AND key_version > 0",
            name="credential_status",
        ),
        schema="catalog",
    )
    op.create_table(
        "deployments",
        *resource_columns(),
        sa.Column("name", sa.String(200), nullable=False, unique=True),
        sa.Column("model_id", sa.Uuid(), sa.ForeignKey("catalog.models.id"), nullable=False),
        sa.Column("upstream_id", sa.Uuid(), sa.ForeignKey("catalog.upstreams.id"), nullable=False),
        sa.Column("credential_id", sa.Uuid(), nullable=False),
        sa.Column("upstream_model", sa.String(200), nullable=False),
        sa.Column("region", sa.String(100), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.ForeignKeyConstraint(
            ["upstream_id", "credential_id"],
            ["catalog.upstream_credentials.upstream_id", "catalog.upstream_credentials.id"],
        ),
        sa.CheckConstraint(
            "status IN ('active','disabled','archived') AND version > 0", name="deployment_status"
        ),
        schema="catalog",
    )
    op.create_table(
        "audit_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("actor_id", sa.Uuid(), nullable=False),
        sa.Column("action", sa.String(100), nullable=False),
        sa.Column("resource_id", sa.Uuid(), nullable=False),
        sa.Column("details", JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        schema="catalog",
    )
    op.create_unique_constraint(
        "api_key_policy_scope_unique",
        "api_keys",
        ["organization_id", "project_id", "id"],
        schema="identity",
    )
    op.create_table(
        "access_policies",
        *resource_columns(),
        sa.Column(
            "organization_id", sa.Uuid(), sa.ForeignKey("identity.organizations.id"), nullable=False
        ),
        sa.Column("layer", sa.String(20), nullable=False),
        sa.Column("scope_type", sa.String(20), nullable=False),
        sa.Column("scope_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid()),
        sa.Column("api_key_id", sa.Uuid()),
        sa.ForeignKeyConstraint(
            ["organization_id", "project_id"],
            ["identity.projects.organization_id", "identity.projects.id"],
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "project_id", "api_key_id"],
            [
                "identity.api_keys.organization_id",
                "identity.api_keys.project_id",
                "identity.api_keys.id",
            ],
        ),
        sa.UniqueConstraint(
            "organization_id", "layer", "scope_type", "scope_id", name="access_policy_scope_unique"
        ),
        sa.CheckConstraint(
            "layer IN ('platform','customer') AND version > 0", name="access_policy_layer"
        ),
        sa.CheckConstraint(
            "(scope_type='organization' AND scope_id=organization_id "
            "AND project_id IS NULL AND api_key_id IS NULL) OR "
            "(scope_type='project' AND scope_id=project_id "
            "AND project_id IS NOT NULL AND api_key_id IS NULL) OR "
            "(scope_type='key' AND scope_id=api_key_id "
            "AND api_key_id IS NOT NULL AND project_id IS NOT NULL)",
            name="access_policy_scope",
        ),
        schema="identity",
    )
    op.create_table(
        "access_policy_models",
        sa.Column(
            "policy_id",
            sa.Uuid(),
            sa.ForeignKey("identity.access_policies.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("model_id", sa.Uuid(), sa.ForeignKey("catalog.models.id"), primary_key=True),
        schema="identity",
    )
    for name in ("models", "upstreams", "upstream_credentials", "deployments"):
        op.create_index(f"{name}_page_idx", name, ["created_at", "id"], schema="catalog")
        op.execute(f"GRANT SELECT, INSERT, UPDATE ON catalog.{name} TO nexus_catalog")
    op.create_index("deployments_model_idx", "deployments", ["model_id"], schema="catalog")
    op.create_index(
        "access_policies_org_page_idx",
        "access_policies",
        ["organization_id", "created_at", "id"],
        schema="identity",
    )
    op.execute("GRANT SELECT, INSERT ON catalog.audit_events TO nexus_catalog")
    op.execute("GRANT SELECT, INSERT, UPDATE ON identity.access_policies TO nexus_identity")
    op.execute("GRANT SELECT, INSERT, DELETE ON identity.access_policy_models TO nexus_identity")


def downgrade():
    op.drop_table("access_policy_models", schema="identity")
    op.drop_table("access_policies", schema="identity")
    op.drop_constraint("api_key_policy_scope_unique", "api_keys", schema="identity")
    for name in ("audit_events", "deployments", "upstream_credentials", "upstreams", "models"):
        op.drop_table(name, schema="catalog")
