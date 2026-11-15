"""Tenant-owned vector stores, measured storage days and native File Search calls."""

from pathlib import Path

from alembic import op

revision = "0026_vector_stores"
down_revision = "0025_openai_files"
branch_labels = None
depends_on = None
SQL = Path(__file__).resolve().parents[1] / "sql"
NEW = SQL / revision
BARGS = (
    "uuid,uuid,uuid,uuid,uuid,uuid,bigint,bigint,integer,text,jsonb,timestamptz,"
    "uuid,uuid,bigint,boolean,integer,integer,numeric,uuid"
)
GARGS = "uuid,uuid,uuid,bigint,bigint,bigint,boolean,integer,integer"
CONTRACTS = [
    (
        "catalog",
        "nexus_catalog",
        "nexus_bridge",
        "eligible.sql",
        ["v1_file_search_eligible(uuid,jsonb,uuid,bigint,bigint,integer,integer)"],
    ),
    (
        "publication",
        "nexus_publication",
        "nexus_bridge",
        "resources.sql",
        [
            "v1_vector_prepare(uuid,uuid,jsonb)",
            "v1_vector(uuid,uuid)",
            "v1_vectors(uuid,uuid,boolean,integer,uuid[])",
            "v1_vector_update(uuid,uuid,text,timestamptz,jsonb,text)",
            "v1_vector_due(uuid,uuid)",
            "v1_vector_mark_missed(uuid,timestamptz)",
            "v1_vector_locator(uuid,uuid,text)",
            "v1_vector_claim()",
            "v1_vector_batch(uuid,uuid,text,boolean)",
            "v1_vector_id(uuid,uuid,text)",
        ],
    ),
    (
        "billing",
        "nexus_billing",
        "nexus_bridge",
        "storage.sql",
        [
            "v1_storage_write(uuid)",
            "v1_storage_missed(uuid,timestamptz)",
            "v1_storage_open(uuid,jsonb,uuid,uuid,uuid,uuid,uuid,bigint)",
            "v1_storage_sample(uuid,timestamptz,bigint,timestamptz,boolean,boolean)",
        ],
    ),
    ("billing", "nexus_billing", "nexus_bridge", "reserve.sql", [f"v1_reserve_tools({BARGS})"]),
    (
        "gateway_v1",
        "nexus_bridge",
        "nexus_gateway",
        "admission.sql",
        ["reserve_file_request(uuid,uuid,uuid,bigint,bigint,bigint,integer,integer)"],
    ),
    (
        "gateway_v1",
        "nexus_bridge",
        "nexus_gateway",
        "gateway.sql",
        [
            "vector_writable(uuid,uuid,bigint)",
            "vector_missed(uuid,timestamptz)",
            "vector_prepare(uuid,uuid,bigint,uuid,uuid)",
            "vector_resolve(uuid,uuid,bigint)",
            "vector_cleanup(uuid,uuid,bigint)",
            "vector_locator(uuid,uuid,text)",
            "vector_page(uuid,bigint,uuid,boolean,integer)",
            "vector_due(uuid,uuid,bigint)",
            "vector_claim()",
            "vector_batch(uuid,uuid,bigint,uuid,text,boolean)",
            "vector_id(uuid,bigint,uuid,uuid,text)",
            "vector_observe(uuid,uuid,bigint,text,timestamptz,jsonb,text,boolean)",
        ],
    ),
]


def original(source, name):
    sql = (SQL / source).read_text()
    begin = sql.index(f"FUNCTION {name}(")
    return "CREATE OR REPLACE " + sql[begin : sql.index("$$;", begin) + 3]


def install(schema, owner, sql):
    op.execute(f"GRANT CREATE ON SCHEMA {schema} TO {owner}")
    op.execute(f"SET LOCAL ROLE {owner}")
    op.get_bind().exec_driver_sql(sql, execution_options={"no_parameters": True})
    op.execute("RESET ROLE")
    op.execute(f"REVOKE CREATE ON SCHEMA {schema} FROM {owner}")


def formats(storage):
    choices = "'openai.responses.v1','anthropic.messages.v1','aws.bedrock.converse.v1'"
    if storage:
        choices += ",'openai.vector_storage.v1'"
    table = "billing.request_attempts"
    name = "request_attempts_expected_usage_format_check"
    op.execute(f"ALTER TABLE {table} DROP CONSTRAINT {name}")
    op.execute(
        f"ALTER TABLE {table} ADD CONSTRAINT {name} CHECK(expected_usage_format IN ({choices}))"
    )


def status_events(vector):
    # Preserve the deduplication counter introduced by 0009 and the call meter added by 0024.
    status = original("0008_gateway_protocol/contracts.sql", "billing.v1_status")
    key = "'output_limit',output_limit,"
    status = status.replace(
        key,
        key
        + "'web_search_limit',web_search_limit,"
        + (
            "'file_search_limit',file_search_limit,'storage_byte_seconds',storage_byte_seconds,"
            "'billing_policy_version',billing_policy_version,"
            if vector
            else ""
        ),
    )
    events = original("0008_gateway_protocol/events.sql", "billing.v1_append").replace(
        "IF FOUND AND old.envelope=e THEN RETURN",
        """IF FOUND AND old.envelope=e THEN
      INSERT INTO billing.ingest_counters(name,total) VALUES('usage',1)
      ON CONFLICT(name) DO UPDATE SET total=billing.ingest_counters.total+1;
      RETURN""",
    )
    fields = (
        "'web_search_requests','file_search_calls','vector_storage_byte_seconds'"
        if vector
        else "'web_search_requests'"
    )
    events = events.replace(
        "WHERE key !~ '^[a-z][a-z0-9_]{0,48}_tokens$'",
        f"WHERE (key NOT IN ({fields}) AND key !~ '^[a-z][a-z0-9_]{{0,48}}_tokens$')",
    )
    install("billing", "nexus_billing", status + "\n" + events)


def upgrade():
    op.get_bind().exec_driver_sql(
        (NEW / "schema.sql").read_text(), execution_options={"no_parameters": True}
    )
    op.execute(
        "GRANT REFERENCES ON billing.price_versions,billing.discount_versions TO nexus_billing"
    )
    op.execute("GRANT USAGE ON SCHEMA publication TO nexus_billing")
    op.execute("GRANT REFERENCES ON publication.config_snapshots TO nexus_billing")
    formats(True)
    status_events(True)
    for schema, owner, grantee, file, names in CONTRACTS:
        code = (NEW / file).read_text()
        for name in names:
            code += f"\nREVOKE ALL ON FUNCTION {schema}.{name} FROM PUBLIC;"
            code += f"\nGRANT EXECUTE ON FUNCTION {schema}.{name} TO {grantee};"
        if file == "storage.sql":
            code += "\nREVOKE ALL ON FUNCTION billing.v1_stage_storage() FROM PUBLIC;"
            code += (
                "\nREVOKE ALL ON FUNCTION billing.v1_storage_writable(uuid,uuid,"
                "uuid,boolean) FROM PUBLIC;"
            )
        if file == "admission.sql":
            code += f"\nREVOKE ALL ON FUNCTION gateway_v1.reserve_tools({GARGS}) FROM PUBLIC;"
        install(schema, owner, code)

    op.execute("REVOKE REFERENCES ON publication.config_snapshots FROM nexus_billing")
    op.execute("REVOKE USAGE ON SCHEMA publication FROM nexus_billing")
    op.execute(
        "REVOKE REFERENCES ON billing.price_versions,billing.discount_versions FROM nexus_billing"
    )


def downgrade():
    op.execute("SET LOCAL ROLE nexus_billing")
    op.execute("""DO $$ BEGIN IF EXISTS(SELECT FROM billing.vector_storage_contracts)
        OR EXISTS(SELECT FROM billing.price_versions WHERE rates ?|
                   array['file_search_calls','vector_storage_byte_seconds'])
        THEN RAISE EXCEPTION 'vector storage usage is retained'; END IF; END $$""")
    op.execute("RESET ROLE")
    install(
        "gateway_v1",
        "nexus_bridge",
        original("0024_web_search/gateway.sql", "gateway_v1.start_attempt")
        + "\n"
        + original("0024_web_search/gateway.sql", "gateway_v1.reserve_metered"),
    )
    install(
        "billing",
        "nexus_billing",
        original("0024_web_search/reserve.sql", "billing.v1_reserve_metered"),
    )
    for schema, owner, _, file, names in reversed(CONTRACTS):
        op.execute(f"SET LOCAL ROLE {owner}")
        for name in reversed(names):
            op.execute(f"DROP FUNCTION {schema}.{name}")
        if file == "gateway.sql":
            op.execute("DROP FUNCTION gateway_v1.vector_binding(uuid,uuid,bigint,boolean)")
        if file == "admission.sql":
            op.execute(f"DROP FUNCTION gateway_v1.reserve_tools({GARGS})")
        if file == "storage.sql":
            op.execute("DROP FUNCTION billing.v1_stage_storage()")
            op.execute("DROP FUNCTION billing.v1_storage_writable(uuid,uuid,uuid,boolean)")
            op.execute(
                "DROP TABLE billing.vector_storage_segments,billing.vector_storage_days,"
                "billing.vector_storage_contracts"
            )
        if file == "resources.sql":
            op.execute("DROP TABLE publication.vector_file_batches,publication.vector_stores")
        op.execute("RESET ROLE")
    status_events(False)
    formats(False)
    op.execute("DROP TRIGGER immutable_vector_bounds ON billing.request_runs")
    op.execute(
        "ALTER TABLE billing.request_runs DROP COLUMN file_search_limit, "
        "DROP COLUMN storage_byte_seconds"
    )
    op.execute("ALTER TABLE catalog.models DROP CONSTRAINT models_tool_dimensions_check")
    op.execute(
        """ALTER TABLE catalog.models ADD CONSTRAINT models_tool_dimensions_check
        CHECK(tool_dimensions IN ('[]','["web_search_requests"]'))"""
    )
