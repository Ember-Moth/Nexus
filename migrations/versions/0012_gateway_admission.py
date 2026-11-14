"""Reduce wallet lock contention during gateway reservation and attempt admission."""

from pathlib import Path

from alembic import op

revision = "0012_gateway_admission"
down_revision = "0011_payment_quotes"
branch_labels = None
depends_on = None
SQL = Path(__file__).resolve().parents[1] / "sql"


def install(source):
    # Both routines retain their v1 signatures, owner, search path and existing ACLs.
    statements = (
        (SQL / source / "admission.sql")
        .read_text()
        .replace("CREATE FUNCTION billing.", "CREATE OR REPLACE FUNCTION billing.")
    )
    op.execute("GRANT CREATE ON SCHEMA billing TO nexus_billing")
    op.execute("SET LOCAL ROLE nexus_billing")
    op.get_bind().exec_driver_sql(statements, execution_options={"no_parameters": True})
    op.execute("RESET ROLE")
    op.execute("REVOKE CREATE ON SCHEMA billing FROM nexus_billing")


def upgrade():
    install(revision)


def downgrade():
    install("0008_gateway_protocol")
