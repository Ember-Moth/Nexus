"""Tenant-owned handles for native stored Responses. Only binding metadata is persisted."""

from pathlib import Path

from alembic import op

from nexus.infrastructure.migrations import references

revision = "0019_stored_responses"
down_revision = "0018_relay_preflight"
branch_labels = None
depends_on = None
SQL = Path(__file__).resolve().parents[1] / "sql" / revision
CONTRACTS = [
    (
        "publication",
        "nexus_publication",
        "nexus_bridge",
        "resources.sql",
        [
            "v1_stored_response(uuid,uuid)",
            "v1_save_response(uuid,uuid,uuid,uuid,jsonb,text)",
            "v1_forget_response(uuid,uuid)",
        ],
    ),
    (
        "billing",
        "nexus_billing",
        "nexus_bridge",
        "bridge.sql",
        ["v1_response_attempt(uuid,uuid,uuid)"],
    ),
    (
        "gateway_v1",
        "nexus_bridge",
        "nexus_gateway",
        "gateway.sql",
        [
            "save_response(uuid,uuid,uuid,text)",
            "resolve_response(uuid,uuid,bigint)",
            "forget_response(uuid,uuid,bigint)",
        ],
    ),
]


def upgrade():
    with references(["publication.gateway_clusters"], "nexus_publication"):
        for schema, owner, grantee, file, functions in CONTRACTS:
            op.execute(f"GRANT CREATE ON SCHEMA {schema} TO {owner}")
            op.execute(f"SET LOCAL ROLE {owner}")
            op.get_bind().exec_driver_sql(
                (SQL / file).read_text(), execution_options={"no_parameters": True}
            )
            for name in functions:
                op.execute(f"REVOKE ALL ON FUNCTION {schema}.{name} FROM PUBLIC")
                op.execute(f"GRANT EXECUTE ON FUNCTION {schema}.{name} TO {grantee}")
            op.execute("RESET ROLE")
            op.execute(f"REVOKE CREATE ON SCHEMA {schema} FROM {owner}")


def downgrade():
    op.execute("SET LOCAL ROLE nexus_publication")
    op.execute("""DO $$ BEGIN IF EXISTS(SELECT FROM publication.stored_responses
        WHERE deleted_at IS NULL)
        THEN RAISE EXCEPTION 'stored responses are retained'; END IF; END $$""")
    op.execute("RESET ROLE")
    for schema, owner, _, _, functions in reversed(CONTRACTS):
        op.execute(f"SET LOCAL ROLE {owner}")
        for name in reversed(functions):
            op.execute(f"DROP FUNCTION {schema}.{name}")
        op.execute("RESET ROLE")
    op.execute("SET LOCAL ROLE nexus_publication")
    op.execute("DROP TABLE publication.stored_responses")
    op.execute("RESET ROLE")
