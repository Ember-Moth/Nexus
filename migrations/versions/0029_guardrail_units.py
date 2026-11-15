"""Meter the policy units reported by Bedrock Converse assessments."""

from pathlib import Path

from alembic import op

revision = "0029_guardrail_units"
down_revision = "0028_container_sessions"
branch_labels = None
depends_on = None
SQL = Path(__file__).resolve().parents[1] / "sql"
BILLING_ARGS = (
    "uuid,uuid,uuid,uuid,uuid,uuid,bigint,bigint,integer,text,jsonb,timestamptz,"
    "uuid,uuid,bigint,boolean,integer,integer,numeric,uuid,text,integer,text,integer,text,integer"
)
GATEWAY_ARGS = (
    "uuid,uuid,uuid,bigint,bigint,bigint,boolean,integer,integer,text,integer,uuid,integer"
)
POLICIES = [
    "guardrail_content_units",
    "guardrail_image_units",
    "guardrail_topic_units",
    "guardrail_sensitive_units",
    "guardrail_grounding_units",
    "guardrail_reasoning_units",
]
EVIDENCE = [
    *POLICIES,
    "guardrail_word_units",
    "guardrail_sensitive_free_units",
    "guardrail_reasoning_text_units",
    "guardrail_reasoning_policies",
    "guardrail_usage_unverified",
]
BASE = [
    "web_search_requests",
    "file_search_calls",
    "vector_storage_byte_seconds",
    "code_execution_seconds",
    "container_1g_minutes",
    "container_4g_minutes",
    "container_16g_minutes",
    "container_64g_minutes",
]


def install(schema, owner, sql):
    op.execute(f"GRANT CREATE ON SCHEMA {schema} TO {owner}")
    op.execute(f"SET LOCAL ROLE {owner}")
    op.get_bind().exec_driver_sql(sql, execution_options={"no_parameters": True})
    op.execute("RESET ROLE")
    op.execute(f"REVOKE CREATE ON SCHEMA {schema} FROM {owner}")


def dimensions(enabled):
    import json

    choices = BASE + POLICIES if enabled else BASE
    op.execute("ALTER TABLE catalog.models DROP CONSTRAINT models_tool_dimensions_check")
    op.execute(
        "ALTER TABLE catalog.models ADD CONSTRAINT models_tool_dimensions_check "
        "CHECK(jsonb_typeof(tool_dimensions)='array' AND "
        f"tool_dimensions <@ '{json.dumps(choices)}')"
    )


def contracts(enabled):
    old = "'container_16g_minutes','container_64g_minutes'"
    keys = ",".join(repr(k) for k in EVIDENCE)
    for sig, changes in [
        (
            "v1_append(text,uuid)",
            [
                (old, f"{old},{keys}"),
                (
                    "FROM jsonb_each(raw))>16",
                    "FROM jsonb_each(raw))>(CASE WHEN body->>'usage_format'="
                    "'aws.bedrock.converse.v1' THEN 24 ELSE 16 END)",
                ),
            ],
        ),
        (
            "v1_status(uuid,uuid)",
            [
                (
                    "'web_search_limit',web_search_limit,",
                    "'guardrail_unit_limit',guardrail_unit_limit,'web_search_limit',web_search_limit,",
                )
            ],
        ),
    ]:
        body = (
            op.get_bind()
            .exec_driver_sql(f"SELECT pg_get_functiondef('billing.{sig}'::regprocedure)")
            .scalar_one()
        )
        for before, after in changes:
            if not enabled:
                before, after = after, before
            if body.count(before) != 1:
                raise ValueError(f"Unexpected billing contract {sig}")
            body = body.replace(before, after)
        install("billing", "nexus_billing", body)


def upgrade():
    dimensions(True)
    op.execute("""ALTER TABLE billing.request_runs ADD COLUMN guardrail_unit_limit integer
        NOT NULL DEFAULT 0 CHECK(guardrail_unit_limit BETWEEN 0 AND 1000000000)""")
    op.execute("""CREATE TRIGGER immutable_guardrail_limit BEFORE UPDATE OF guardrail_unit_limit
        ON billing.request_runs FOR EACH ROW
        WHEN (OLD.guardrail_unit_limit<>NEW.guardrail_unit_limit)
        EXECUTE FUNCTION billing.protect_search_limit()""")
    op.execute("""CREATE TABLE billing.guardrail_usage_receipts(
        attempt_id uuid PRIMARY KEY REFERENCES billing.request_attempts,
        supplier_id uuid NOT NULL, reference_digest varchar(64) NOT NULL,
        quantities jsonb NOT NULL, UNIQUE(supplier_id,reference_digest))""")
    op.execute("GRANT SELECT,INSERT ON billing.guardrail_usage_receipts TO nexus_billing")
    contracts(True)
    name = f"billing.v1_reserve_guarded_capacity({BILLING_ARGS})"
    install(
        "billing",
        "nexus_billing",
        (SQL / revision / "reserve.sql").read_text()
        + f"""
        REVOKE ALL ON FUNCTION {name} FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION {name} TO nexus_bridge;""",
    )
    name = "gateway_v1.reserve_guardrail_request(uuid,uuid,uuid,bigint,bigint,bigint,uuid,integer)"
    install(
        "gateway_v1",
        "nexus_bridge",
        (SQL / revision / "gateway.sql").read_text()
        + f"""
        REVOKE ALL ON FUNCTION gateway_v1.reserve_with_guardrail({GATEWAY_ARGS}) FROM PUBLIC;
        REVOKE ALL ON FUNCTION {name} FROM PUBLIC;
        GRANT EXECUTE ON FUNCTION {name} TO nexus_gateway;""",
    )


def prior(path, name):
    sql = (SQL / path).read_text()
    a = sql.index(f"FUNCTION {name}(")
    return "CREATE OR REPLACE " + sql[a : sql.index("$$;", a) + 3]


def downgrade():
    # Old workers cannot reconcile these prices, frozen bounds, or observed counters.
    keys = ",".join(repr(k) for k in EVIDENCE)
    op.execute("SET LOCAL ROLE nexus_catalog")
    op.execute(f"""DO $$ BEGIN
      IF EXISTS(SELECT FROM catalog.models WHERE tool_dimensions ?| array[{keys}])
      OR EXISTS(SELECT FROM catalog.deployments
           WHERE coalesce((relay_profile->>'bedrock_guardrail_unit_ceiling')::integer,0)>0)
      THEN RAISE EXCEPTION 'Guardrail pricing is configured'; END IF; END $$""")
    op.execute("RESET ROLE")
    op.execute("SET LOCAL ROLE nexus_billing")
    op.execute(f"""DO $$ BEGIN
      IF EXISTS(SELECT FROM billing.request_runs WHERE guardrail_unit_limit>0)
      OR EXISTS(SELECT FROM billing.guardrail_usage_receipts)
      OR EXISTS(SELECT FROM billing.price_versions WHERE rates ?| array[{keys}])
      OR EXISTS(SELECT FROM billing.usage_events
                WHERE envelope#>'{{payload,raw_usage}}' ?| array[{keys}])
      THEN RAISE EXCEPTION 'guardrail usage is retained'; END IF; END $$""")
    op.execute("RESET ROLE")
    path = "0027_execution_containers/admission.sql"
    install(
        "gateway_v1",
        "nexus_bridge",
        prior(path, "gateway_v1.start_attempt")
        + prior(path, "gateway_v1.reserve_with_execution")
        + f"""
        DROP FUNCTION gateway_v1.reserve_guardrail_request(
            uuid,uuid,uuid,bigint,bigint,bigint,uuid,integer);
        DROP FUNCTION gateway_v1.reserve_with_guardrail({GATEWAY_ARGS});""",
    )
    install(
        "billing",
        "nexus_billing",
        prior("0028_container_sessions/reserve.sql", "billing.v1_reserve_capacity")
        + f"\nDROP FUNCTION billing.v1_reserve_guarded_capacity({BILLING_ARGS});",
    )
    op.execute("DROP TABLE billing.guardrail_usage_receipts")
    contracts(False)
    op.execute("DROP TRIGGER immutable_guardrail_limit ON billing.request_runs")
    op.execute("ALTER TABLE billing.request_runs DROP COLUMN guardrail_unit_limit")
    dimensions(False)
