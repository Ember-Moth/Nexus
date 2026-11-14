"""Provision a permanent personal organization and enable scoped invitations."""

from pathlib import Path

from alembic import op

revision = "0015_personal_organizations"
down_revision = "0014_bedrock_converse"
branch_labels = None
depends_on = None


def upgrade():
    sql = Path(__file__).resolve().parents[1] / "sql" / revision / "personal.sql"
    op.get_bind().exec_driver_sql(sql.read_text(), execution_options={"no_parameters": True})


def downgrade():
    op.execute("""DO $$ BEGIN IF EXISTS(SELECT FROM identity.organization_invitations)
        THEN RAISE EXCEPTION 'organization invitations are retained'; END IF; END $$""")
    op.execute("DROP TABLE identity.organization_invitations")
    for table, trigger, function in [
        ("memberships", "personal_membership_binding", "protect_personal_membership"),
        ("organizations", "personal_organization_binding", "protect_personal_organization"),
        ("users", "user_personal_organization", "on_user_created"),
    ]:
        op.execute(f"DROP TRIGGER {trigger} ON identity.{table}")
        op.execute(f"DROP FUNCTION identity.{function}()")
    op.execute("DROP FUNCTION identity.create_personal_organization(uuid)")
    op.execute("ALTER TABLE identity.organizations DROP COLUMN personal_owner_id")
