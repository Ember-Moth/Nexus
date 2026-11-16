"""Per-container time holds, reuse and supplier-evidence reconciliation."""

from pathlib import Path

from alembic import op

from nexus.infrastructure.migrations import references

revision = "0028_container_sessions"
down_revision = "0027_execution_containers"
branch_labels = None
depends_on = None
SQL = Path(__file__).resolve().parents[1] / "sql"
D = SQL / revision
ARGS = (
    "uuid,uuid,uuid,uuid,uuid,uuid,bigint,bigint,integer,text,jsonb,timestamptz,"
    "uuid,uuid,bigint,boolean,integer,integer,numeric,uuid,text,integer,text,integer,text"
)
CONTRACTS = [
    (
        "publication",
        "nexus_publication",
        "nexus_bridge",
        "resources.sql",
        [
            "v1_container_operation(uuid,uuid,bigint,jsonb,uuid,text,boolean)",
            "v1_container_origin(uuid)",
            "v1_container_cleanup(uuid,uuid,uuid,bigint)",
            "v1_stored_interpreter(uuid,uuid)",
            "v1_container_attest(uuid,uuid)",
            "v1_containers(uuid,uuid,boolean,integer,uuid[])",
        ],
    ),
    ("billing", "nexus_billing", "nexus_bridge", "reserve.sql", [f"v1_reserve_capacity({ARGS})"]),
    (
        "billing",
        "nexus_billing",
        "nexus_bridge",
        "billing.sql",
        [
            "v1_container_hold(uuid,uuid,jsonb,uuid,uuid,uuid,jsonb,text,text,integer,uuid,uuid,bigint,boolean,uuid)",
            "v1_container_link(uuid,uuid,uuid,uuid,text,integer,boolean,text,boolean)",
            "v1_container_release(uuid,uuid,uuid)",
            "v1_container_due(uuid,uuid,boolean)",
        ],
    ),
    (
        "gateway_v1",
        "nexus_bridge",
        "nexus_gateway",
        "gateway.sql",
        [
            "container_admit(uuid,uuid,bigint,uuid,uuid,uuid,text,boolean,boolean)",
            "reserve_interpreter(uuid,uuid,uuid,bigint,bigint,bigint,integer,integer,uuid,uuid,text,boolean)",
            "container_receipt(uuid,uuid,text,jsonb,boolean)",
            "container_no_use(uuid,uuid,boolean)",
            "container_cleanup(uuid,uuid,uuid,bigint)",
            "container_retired(uuid,uuid,text,boolean)",
            "stored_interpreter(uuid,uuid,bigint)",
            "containers(uuid,bigint,uuid,boolean,integer)",
        ],
    ),
]


def install(schema, owner, sql):
    op.execute(f"GRANT CREATE ON SCHEMA {schema} TO {owner}")
    op.execute(f"SET LOCAL ROLE {owner}")
    op.get_bind().exec_driver_sql(sql, execution_options={"no_parameters": True})
    op.execute("RESET ROLE")
    op.execute(f"REVOKE CREATE ON SCHEMA {schema} FROM {owner}")


def original(path, name):
    sql = (SQL / path).read_text()
    a = sql.index(f"FUNCTION {name}(")
    return "CREATE OR REPLACE " + sql[a : sql.index("$$;", a) + 3]


def formats(on):
    name = "request_attempts_expected_usage_format_check"
    values = [
        "openai.responses.v1",
        "anthropic.messages.v1",
        "aws.bedrock.converse.v1",
        "openai.vector_storage.v1",
    ]
    if on:
        values.append("openai.container.v1")
    op.execute(f"ALTER TABLE billing.request_attempts DROP CONSTRAINT {name}")
    op.execute(
        f"ALTER TABLE billing.request_attempts ADD CONSTRAINT {name} "
        f"CHECK(expected_usage_format IN ({','.join(repr(v) for v in values)}))"
    )


def usage_fields(enabled):
    signature = "billing.v1_append(text,uuid)"
    sql = (
        op.get_bind()
        .exec_driver_sql(f"SELECT pg_get_functiondef('{signature}'::regprocedure)")
        .scalar_one()
    )
    old = "'vector_storage_byte_seconds','code_execution_requests'"
    new = (
        old + ",'container_1g_minutes','container_4g_minutes',"
        "'container_16g_minutes','container_64g_minutes'"
    )
    if not enabled:
        old, new = new, old
    if sql.count(old) != 1:
        raise ValueError("Unexpected usage envelope")
    install("billing", "nexus_billing", sql.replace(old, new))


def upgrade():
    op.get_bind().exec_driver_sql(
        (D / "schema.sql").read_text(), execution_options={"no_parameters": True}
    )
    formats(True)
    usage_fields(True)
    with references(["billing.price_versions"], "nexus_billing"):
        for schema, owner, grantee, file, names in CONTRACTS:
            code = (D / file).read_text()
            for name in names:
                code += f"\nREVOKE ALL ON FUNCTION {schema}.{name} FROM PUBLIC;"
                code += f"\nGRANT EXECUTE ON FUNCTION {schema}.{name} TO {grantee};"
            if file == "billing.sql":
                code += (
                    "\nREVOKE ALL ON FUNCTION billing.v1_review_container(),"
                    "billing.v1_container_settled() FROM PUBLIC;"
                )
            install(schema, owner, code)
    op.execute("SET LOCAL ROLE nexus_billing")
    op.execute("GRANT EXECUTE ON FUNCTION billing.v1_container_settled() TO nexus_migrator")
    op.execute("RESET ROLE")
    op.execute("""CREATE TRIGGER container_settled AFTER UPDATE OF reservation_state
      ON billing.request_runs
      FOR EACH ROW WHEN (NEW.billing_policy_version='openai_container_v1' AND
                         NEW.reservation_state<>'held')
      EXECUTE FUNCTION billing.v1_container_settled()""")
    op.execute("SET LOCAL ROLE nexus_billing")
    op.execute("REVOKE EXECUTE ON FUNCTION billing.v1_container_settled() FROM nexus_migrator")
    op.execute("RESET ROLE")


def downgrade():
    op.execute("SET LOCAL ROLE nexus_billing")
    op.execute("""DO $$ BEGIN IF EXISTS(SELECT FROM billing.container_sessions) OR EXISTS(
      SELECT FROM billing.price_versions WHERE rates ?| array['container_1g_minutes',
        'container_4g_minutes','container_16g_minutes','container_64g_minutes'])
      THEN RAISE EXCEPTION 'container usage is retained'; END IF; END $$""")
    op.execute("RESET ROLE")
    op.execute("DROP TRIGGER container_settled ON billing.request_runs")
    install(
        "gateway_v1",
        "nexus_bridge",
        original("0027_execution_containers/gateway.sql", "gateway_v1.container_remove")
        + "\n"
        + original("0008_gateway_protocol/bridge.sql", "gateway_v1.cancel_unstarted_request"),
    )
    install(
        "billing",
        "nexus_billing",
        original("0027_execution_containers/reserve.sql", "billing.v1_reserve_execution"),
    )
    for schema, owner, _, file, names in reversed(CONTRACTS):
        op.execute(f"SET LOCAL ROLE {owner}")
        for name in reversed(names):
            op.execute(f"DROP FUNCTION {schema}.{name}")
        if file == "resources.sql":
            op.execute("DROP TABLE publication.container_operations")
        if file == "billing.sql":
            op.execute("DROP FUNCTION billing.v1_review_container(),billing.v1_container_settled()")
            op.execute("DROP TABLE billing.container_operations,billing.container_sessions")
        op.execute("RESET ROLE")
    usage_fields(False)
    formats(False)
    op.execute(
        "ALTER TABLE billing.execution_usage_receipts ALTER COLUMN confirmed_seconds TYPE integer"
    )
    op.execute("DROP TRIGGER immutable_container_limits ON billing.request_runs")
    op.execute(
        "ALTER TABLE billing.request_runs DROP COLUMN container_origin,"
        "DROP COLUMN container_handle,DROP COLUMN container_unit,"
        "DROP COLUMN container_minutes_limit,DROP COLUMN container_tariff"
    )
    op.execute("ALTER TABLE catalog.models DROP CONSTRAINT models_tool_dimensions_check")
    op.execute("""ALTER TABLE catalog.models ADD CONSTRAINT models_tool_dimensions_check
        CHECK(jsonb_typeof(tool_dimensions)='array' AND
        tool_dimensions <@ '["web_search_requests","file_search_calls",
                             "vector_storage_byte_seconds","code_execution_seconds"]')""")
