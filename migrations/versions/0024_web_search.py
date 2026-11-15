"""Freeze Web Search call bounds and price token and per-call usage independently."""

from pathlib import Path

from alembic import op

revision = "0024_web_search"
down_revision = "0023_conversations"
branch_labels = None
depends_on = None
SQL = Path(__file__).resolve().parents[1] / "sql"
BILLING_ARGS = (
    "uuid,uuid,uuid,uuid,uuid,uuid,bigint,bigint,integer,text,jsonb,"
    "timestamptz,uuid,uuid,bigint,boolean,integer"
)
SEARCH_ARGS = "uuid,uuid,uuid,bigint,bigint,bigint,integer"
GATEWAY_ARGS = "uuid,uuid,uuid,bigint,bigint,bigint,boolean,integer"


def original(source, name):
    text = (SQL / source).read_text()
    start = text.index(f"FUNCTION {name}(")
    end = text.index("$$;", start) + 3
    return "CREATE OR REPLACE " + text[start:end]


def install(schema, owner, statements):
    op.execute(f"GRANT CREATE ON SCHEMA {schema} TO {owner}")
    op.execute(f"SET LOCAL ROLE {owner}")
    op.get_bind().exec_driver_sql(statements, execution_options={"no_parameters": True})
    op.execute("RESET ROLE")
    op.execute(f"REVOKE CREATE ON SCHEMA {schema} FROM {owner}")


def contracts(enabled):
    eligible = original("0013_relay_contract/eligible.sql", "catalog.v1_eligible")
    status = original("0008_gateway_protocol/contracts.sql", "billing.v1_status")
    events = original("0008_gateway_protocol/events.sql", "billing.v1_append").replace(
        "IF FOUND AND old.envelope=e THEN RETURN",
        """IF FOUND AND old.envelope=e THEN
          INSERT INTO billing.ingest_counters(name,total) VALUES('usage',1)
          ON CONFLICT(name) DO UPDATE SET total=billing.ingest_counters.total+1;
          RETURN""",
    )
    if enabled:
        eligible = eligible.replace(
            "RETURN m.token_dimensions;", "RETURN m.token_dimensions||m.tool_dimensions;"
        )
        status = status.replace(
            "'output_limit',output_limit,",
            "'output_limit',output_limit,'web_search_limit',web_search_limit,",
        )
        events = events.replace(
            "WHERE key !~ '^[a-z][a-z0-9_]{0,48}_tokens$'",
            "WHERE (key <> 'web_search_requests' AND key !~ '^[a-z][a-z0-9_]{0,48}_tokens$')",
        )
    install("catalog", "nexus_catalog", eligible)
    install("billing", "nexus_billing", status + "\n" + events)


def upgrade():
    op.execute("""ALTER TABLE catalog.models ADD COLUMN tool_dimensions jsonb NOT NULL DEFAULT '[]'
               CHECK(tool_dimensions IN ('[]','["web_search_requests"]'))""")
    op.execute("""ALTER TABLE billing.request_runs
               ADD COLUMN web_search_limit integer NOT NULL DEFAULT 0
               CHECK(web_search_limit BETWEEN 0 AND 1000000000)""")
    op.execute("""CREATE FUNCTION billing.protect_search_limit() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'request admission is immutable' USING ERRCODE='23514'; END $$""")
    op.execute("REVOKE ALL ON FUNCTION billing.protect_search_limit() FROM PUBLIC")
    op.execute("""CREATE TRIGGER protect_search_limit BEFORE UPDATE OF web_search_limit
         ON billing.request_runs
         FOR EACH ROW WHEN (OLD.web_search_limit<>NEW.web_search_limit)
         EXECUTE FUNCTION billing.protect_search_limit()""")
    contracts(True)
    signature = "catalog.v1_search_eligible(uuid,jsonb,uuid,bigint,bigint,integer)"
    install(
        "catalog",
        "nexus_catalog",
        (SQL / revision / "eligible.sql").read_text()
        + f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC;"
        + f"GRANT EXECUTE ON FUNCTION {signature} TO nexus_bridge;",
    )
    install(
        "billing",
        "nexus_billing",
        (SQL / revision / "reserve.sql").read_text()
        + f"""
        REVOKE ALL ON FUNCTION billing.v1_reserve_metered({BILLING_ARGS}) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION billing.v1_reserve_metered({BILLING_ARGS}) TO nexus_bridge;
    """,
    )
    install(
        "gateway_v1",
        "nexus_bridge",
        (SQL / revision / "gateway.sql").read_text()
        + f"""
        REVOKE ALL ON FUNCTION gateway_v1.reserve_metered({GATEWAY_ARGS}) FROM PUBLIC;
        REVOKE ALL ON FUNCTION gateway_v1.reserve_search_request({SEARCH_ARGS}) FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION gateway_v1.reserve_search_request({SEARCH_ARGS}) TO nexus_gateway;
    """,
    )


def downgrade():
    op.execute("""DO $$ BEGIN IF EXISTS(SELECT FROM billing.request_runs WHERE web_search_limit>0)
        OR EXISTS(SELECT FROM billing.price_versions WHERE rates ? 'web_search_requests')
        OR EXISTS(SELECT FROM catalog.models WHERE tool_dimensions <> '[]')
        THEN RAISE EXCEPTION 'web search pricing is in use'; END IF; END $$""")
    install(
        "gateway_v1",
        "nexus_bridge",
        "\n".join(
            [
                original("0022_message_batches/gateway.sql", "gateway_v1.reserve_item"),
                original("0022_message_batches/gateway.sql", "gateway_v1.batch_prepare"),
                original("0013_relay_contract/start_attempt.sql", "gateway_v1.start_attempt"),
                f"DROP FUNCTION gateway_v1.reserve_search_request({SEARCH_ARGS});",
                f"DROP FUNCTION gateway_v1.reserve_metered({GATEWAY_ARGS});",
            ]
        ),
    )
    install(
        "billing",
        "nexus_billing",
        original("0022_message_batches/billing.sql", "billing.v1_reserve_item")
        + f"\nDROP FUNCTION billing.v1_reserve_metered({BILLING_ARGS});",
    )
    install(
        "catalog",
        "nexus_catalog",
        "DROP FUNCTION catalog.v1_search_eligible(uuid,jsonb,uuid,bigint,bigint,integer);",
    )
    contracts(False)
    op.execute("DROP TRIGGER protect_search_limit ON billing.request_runs")
    op.execute("DROP FUNCTION billing.protect_search_limit()")
    op.execute("ALTER TABLE billing.request_runs DROP COLUMN web_search_limit")
    op.execute("ALTER TABLE catalog.models DROP COLUMN tool_dimensions")
