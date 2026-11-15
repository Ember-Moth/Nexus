"""Authorize non-generating relay calls against current tenant and deployment state."""

from pathlib import Path

from alembic import op

revision = "0018_relay_preflight"
down_revision = "0017_request_correlation"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("GRANT CREATE ON SCHEMA catalog TO nexus_catalog")
    op.execute("SET LOCAL ROLE nexus_catalog")
    op.execute("""CREATE FUNCTION catalog.v1_model_created(model uuid) RETURNS bigint
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
        SELECT floor(extract(epoch FROM created_at))::bigint FROM catalog.models WHERE id=model
        $$""")
    op.execute("REVOKE ALL ON FUNCTION catalog.v1_model_created(uuid) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION catalog.v1_model_created(uuid) TO nexus_bridge")
    op.execute("RESET ROLE")
    op.execute("REVOKE CREATE ON SCHEMA catalog FROM nexus_catalog")
    op.execute("GRANT CREATE ON SCHEMA gateway_v1 TO nexus_bridge")
    op.execute("SET LOCAL ROLE nexus_bridge")
    path = Path(__file__).resolve().parents[1] / "sql" / revision / "preflight.sql"
    op.get_bind().exec_driver_sql(path.read_text(), execution_options={"no_parameters": True})
    op.execute("RESET ROLE")
    op.execute("REVOKE CREATE ON SCHEMA gateway_v1 FROM nexus_bridge")


def downgrade():
    op.execute("DROP FUNCTION gateway_v1.preflight(uuid,bigint,uuid,uuid)")
    op.execute("DROP FUNCTION catalog.v1_model_created(uuid)")
