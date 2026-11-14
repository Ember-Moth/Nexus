"""Bind native relay usage to the authorized deployment revision."""

from pathlib import Path

from alembic import op

revision = "0013_relay_contract"
down_revision = "0012_gateway_admission"
branch_labels = None
depends_on = None
SQL = Path(__file__).resolve().parents[1] / "sql"
ARGS = "uuid,uuid,uuid,uuid,uuid,integer,timestamptz,uuid"


def install(owner, schema, statements):
    op.execute(f"GRANT CREATE ON SCHEMA {schema} TO {owner}")
    op.execute(f"SET LOCAL ROLE {owner}")
    op.get_bind().exec_driver_sql(statements, execution_options={"no_parameters": True})
    op.execute("RESET ROLE")
    op.execute(f"REVOKE CREATE ON SCHEMA {schema} FROM {owner}")


def previous_function(source, name):
    text = source.read_text()
    start = text.index(f"FUNCTION {name}(")
    end = text.index("END $$;", start) + len("END $$;")
    return "CREATE OR REPLACE " + text[start:end]


def upgrade():
    op.execute("ALTER TABLE catalog.deployments ADD COLUMN relay_profile jsonb")
    op.execute("""
        ALTER TABLE billing.request_attempts ADD COLUMN expected_usage_format varchar(80)
        CHECK (expected_usage_format IN ('openai.responses.v1','anthropic.messages.v1'))
    """)
    install(
        "nexus_billing",
        "billing",
        f"DROP FUNCTION billing.v1_attempt({ARGS});\n"
        + (SQL / revision / "attempt.sql").read_text()
        + f"REVOKE ALL ON FUNCTION billing.v1_attempt({ARGS},text) FROM PUBLIC;\n"
        + f"GRANT EXECUTE ON FUNCTION billing.v1_attempt({ARGS},text) TO nexus_bridge",
    )
    install("nexus_catalog", "catalog", (SQL / revision / "eligible.sql").read_text())
    install("nexus_bridge", "gateway_v1", (SQL / revision / "start_attempt.sql").read_text())


def downgrade():
    op.execute("""DO $$ BEGIN IF EXISTS (
        SELECT FROM billing.request_attempts WHERE expected_usage_format IS NOT NULL
    ) OR EXISTS (SELECT FROM catalog.deployments WHERE relay_profile IS NOT NULL)
    OR EXISTS (SELECT FROM publication.config_snapshots WHERE
        manifest->'required_capabilities' ? 'relay.native.v1') THEN
        RAISE EXCEPTION 'native relay records are retained';
    END IF; END $$""")
    prior = SQL / "0008_gateway_protocol"
    install(
        "nexus_bridge",
        "gateway_v1",
        previous_function(prior / "bridge.sql", "gateway_v1.start_attempt"),
    )
    install(
        "nexus_catalog",
        "catalog",
        previous_function(prior / "contracts.sql", "catalog.v1_eligible"),
    )
    install(
        "nexus_billing",
        "billing",
        f"DROP FUNCTION billing.v1_attempt({ARGS},text);\n"
        + previous_function(SQL / down_revision / "admission.sql", "billing.v1_attempt")
        + f"REVOKE ALL ON FUNCTION billing.v1_attempt({ARGS}) FROM PUBLIC;\n"
        + f"GRANT EXECUTE ON FUNCTION billing.v1_attempt({ARGS}) TO nexus_bridge",
    )
    op.execute("ALTER TABLE billing.request_attempts DROP COLUMN expected_usage_format")
    op.execute("ALTER TABLE catalog.deployments DROP COLUMN relay_profile")
