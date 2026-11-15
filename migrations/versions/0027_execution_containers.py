"""Tenant-owned execution containers and generated file bindings."""

from pathlib import Path

from alembic import op

revision = "0027_execution_containers"
down_revision = "0026_vector_stores"
branch_labels = None
depends_on = None
SQL = Path(__file__).resolve().parents[1] / "sql" / revision
BARGS = (
    "uuid,uuid,uuid,uuid,uuid,uuid,bigint,bigint,integer,text,jsonb,timestamptz,"
    "uuid,uuid,bigint,boolean,integer,integer,numeric,uuid,text,integer"
)
GARGS = "uuid,uuid,uuid,bigint,bigint,bigint,boolean,integer,integer,text,integer"
CONTRACTS = [
    (
        "catalog",
        "nexus_catalog",
        "nexus_bridge",
        "eligible.sql",
        ["v1_execution_eligible(uuid,jsonb,uuid,bigint,bigint,integer,text,integer)"],
    ),
    (
        "billing",
        "nexus_billing",
        "nexus_bridge",
        "reserve.sql",
        [
            f"v1_reserve_execution({BARGS})",
            "v1_execution_supplier(uuid,uuid,uuid,uuid)",
            "v1_execution_attempt(uuid,uuid,uuid,uuid)",
        ],
    ),
    (
        "gateway_v1",
        "nexus_bridge",
        "nexus_gateway",
        "admission.sql",
        ["reserve_execution_request(uuid,uuid,uuid,bigint,bigint,bigint,integer,text,integer)"],
    ),
    (
        "publication",
        "nexus_publication",
        "nexus_bridge",
        "resources.sql",
        [
            "v1_container(uuid,uuid,text)",
            "v1_container_bind(uuid,jsonb,text,text,jsonb,boolean)",
            "v1_container_remove(uuid,uuid)",
            "v1_batch_execution(uuid,text,uuid)",
            "v1_container_file_bind(uuid,uuid,text,boolean)",
            "v1_container_file(uuid,uuid,uuid,boolean)",
            "v1_execution_file(uuid,jsonb,text,boolean)",
            "v1_execution_file_refresh(uuid,uuid,jsonb)",
        ],
    ),
    (
        "gateway_v1",
        "nexus_bridge",
        "nexus_gateway",
        "gateway.sql",
        [
            "container_bind(uuid,bigint,uuid,uuid,text,text,jsonb,boolean)",
            "container_resolve(uuid,uuid,bigint,text)",
            "container_remove(uuid,uuid,bigint)",
            "batch_execution(uuid,text,uuid,bigint)",
            "container_file_bind(uuid,uuid,bigint,text,boolean)",
            "container_file(uuid,uuid,bigint,uuid,boolean)",
            "execution_file(uuid,bigint,uuid,uuid,text,boolean)",
            "execution_file_refresh(uuid,uuid,bigint,jsonb)",
            "execution_output(uuid,uuid,uuid,text,boolean)",
        ],
    ),
]


def event_contracts(up):
    changes = [
        (
            "billing.v1_status(uuid,uuid)",
            "'web_search_limit',web_search_limit,",
            "'execution_mode',execution_mode,'execution_seconds_limit',execution_seconds_limit,'web_search_limit',web_search_limit,",
        ),
        (
            "billing.v1_append(text,uuid)",
            "'file_search_calls','vector_storage_byte_seconds'",
            "'file_search_calls','vector_storage_byte_seconds','code_execution_requests'",
        ),
    ]
    op.execute("GRANT CREATE ON SCHEMA billing TO nexus_billing")
    op.execute("SET LOCAL ROLE nexus_billing")
    for signature, old, new in changes:
        sql = (
            op.get_bind()
            .exec_driver_sql(f"SELECT pg_get_functiondef('{signature}'::regprocedure)")
            .scalar_one()
        )
        if not up:
            old, new = new, old
        if sql.count(old) != 1:
            raise ValueError(f"Unexpected contract {signature}")
        op.get_bind().exec_driver_sql(
            sql.replace(old, new), execution_options={"no_parameters": True}
        )
    op.execute("RESET ROLE")
    op.execute("REVOKE CREATE ON SCHEMA billing FROM nexus_billing")


def restore(schema, owner, source, name):
    path = SQL.parent / source
    contents = path.read_text()
    start = contents.index(f"FUNCTION {schema}.{name}(")
    code = "CREATE OR REPLACE " + contents[start : contents.index("$$;", start) + 3]
    op.execute(f"GRANT CREATE ON SCHEMA {schema} TO {owner}")
    op.execute(f"SET LOCAL ROLE {owner}")
    op.get_bind().exec_driver_sql(code, execution_options={"no_parameters": True})
    op.execute("RESET ROLE")
    op.execute(f"REVOKE CREATE ON SCHEMA {schema} FROM {owner}")


def upgrade():
    op.get_bind().exec_driver_sql(
        (SQL / "schema.sql").read_text(), execution_options={"no_parameters": True}
    )
    event_contracts(True)
    for schema, owner, grantee, file, names in CONTRACTS:
        op.execute(f"GRANT CREATE ON SCHEMA {schema} TO {owner}")
        op.execute(f"SET LOCAL ROLE {owner}")
        op.get_bind().exec_driver_sql(
            (SQL / file).read_text(), execution_options={"no_parameters": True}
        )
        if file == "admission.sql":
            op.execute(
                f"REVOKE ALL ON FUNCTION gateway_v1.reserve_with_execution({GARGS}) FROM PUBLIC"
            )
        for name in names:
            op.execute(f"REVOKE ALL ON FUNCTION {schema}.{name} FROM PUBLIC")
            op.execute(f"GRANT EXECUTE ON FUNCTION {schema}.{name} TO {grantee}")
        op.execute("RESET ROLE")
        op.execute(f"REVOKE CREATE ON SCHEMA {schema} FROM {owner}")


def downgrade():
    op.execute("SET LOCAL ROLE nexus_publication")
    op.execute("""DO $$ BEGIN IF EXISTS(SELECT FROM publication.execution_containers
        WHERE deleted_at IS NULL)
               THEN RAISE EXCEPTION 'execution containers are retained'; END IF; END $$""")
    op.execute("RESET ROLE")
    op.execute("""DO $$ BEGIN IF EXISTS(SELECT FROM billing.request_runs
                    WHERE execution_mode<>'none')
         OR EXISTS(SELECT FROM billing.price_versions WHERE rates ? 'code_execution_seconds')
         THEN RAISE EXCEPTION 'execution pricing is retained'; END IF; END $$""")
    restore("gateway_v1", "nexus_bridge", "0026_vector_stores/admission.sql", "start_attempt")
    restore("gateway_v1", "nexus_bridge", "0024_web_search/gateway.sql", "batch_prepare")
    restore("gateway_v1", "nexus_bridge", "0026_vector_stores/admission.sql", "reserve_tools")
    restore("billing", "nexus_billing", "0026_vector_stores/reserve.sql", "v1_reserve_tools")
    for schema, owner, _, file, names in reversed(CONTRACTS):
        op.execute(f"SET LOCAL ROLE {owner}")
        for name in reversed(names):
            op.execute(f"DROP FUNCTION {schema}.{name}")
        if file == "admission.sql":
            op.execute(f"DROP FUNCTION gateway_v1.reserve_with_execution({GARGS})")
        if file == "gateway.sql":
            op.execute("DROP FUNCTION gateway_v1.execution_deployment(uuid,bigint,uuid,uuid,text)")
        op.execute("RESET ROLE")
    op.execute("SET LOCAL ROLE nexus_publication")
    op.execute("DROP TABLE publication.container_files,publication.execution_containers")
    op.execute("RESET ROLE")
    event_contracts(False)
    op.execute("DROP TRIGGER immutable_execution_bounds ON billing.request_runs")
    op.execute("DROP TABLE billing.execution_usage_receipts")
    op.execute("ALTER TABLE billing.request_attempts DROP COLUMN meter_supplier_id")
    op.execute(
        "ALTER TABLE billing.request_runs DROP COLUMN execution_mode, "
        "DROP COLUMN execution_seconds_limit"
    )
    op.execute("ALTER TABLE catalog.models DROP CONSTRAINT models_tool_dimensions_check")
    op.execute("""ALTER TABLE catalog.models ADD CONSTRAINT models_tool_dimensions_check CHECK(
      jsonb_typeof(tool_dimensions)='array' AND
      tool_dimensions <@ '["web_search_requests","file_search_calls",
                          "vector_storage_byte_seconds"]')""")
