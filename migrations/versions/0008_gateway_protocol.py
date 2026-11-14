"""Version-one control/data plane contract and durable settlement."""

from pathlib import Path

from alembic import op

revision = "0008_gateway_protocol"
down_revision = "0007_payment_admission"
branch_labels = None
depends_on = None
SQL = Path(__file__).resolve().parents[1] / "sql" / revision
SOURCES = {
    "identity": (
        "organizations",
        "projects",
        "service_accounts",
        "api_keys",
        "access_policies",
        "access_policy_models",
    ),
    "catalog": (
        "models",
        "upstreams",
        "deployments",
        "upstream_credentials",
        "route_policies",
        "quota_policies",
    ),
    "billing": ("price_versions", "discount_versions"),
}


# Only these signatures are part of the privilege contract.
FUNCTIONS = {
    "identity": {"v1_authorize": "uuid,uuid"},
    "catalog": {
        "v1_eligible": "uuid,uuid,jsonb,uuid,bigint,bigint",
        "v1_credential_current": "uuid,integer",
    },
    "billing": {
        "v1_reserve": "uuid,uuid,uuid,uuid,uuid,uuid,bigint,bigint,integer,text,"
        "jsonb,timestamptz,uuid,uuid,bigint",
        "v1_attempt": "uuid,uuid,uuid,uuid,uuid,integer,timestamptz,uuid",
        "v1_status": "uuid,uuid",
        "v1_cancel": "uuid,uuid,text",
        "v1_append": "text,uuid",
    },
    "publication": {
        "v1_principal": "boolean",
        "v1_snapshot": "bigint,boolean",
        "v1_envelope": "bigint,uuid,integer",
        "v1_ack": "bigint,text,boolean,text",
    },
    "gateway_v1": {
        "get_config_snapshot": "bigint",
        "get_credential_envelope": "bigint,uuid,integer",
        "ack_config": "bigint,text,boolean,text",
        "reserve_request": "uuid,uuid,uuid,bigint,bigint,bigint",
        "get_request_status": "uuid",
        "start_attempt": "uuid,uuid,integer,uuid",
        "append_usage_event": "text",
        "cancel_unstarted_request": "uuid,text",
    },
}


def signatures(schema):
    return [f"{schema}.{name}({args})" for name, args in FUNCTIONS[schema].items()]


def run_file(name):
    op.get_bind().exec_driver_sql(
        (SQL / name).read_text(), execution_options={"no_parameters": True}
    )


def upgrade():
    roles = (
        op.get_bind()
        .exec_driver_sql("""
        SELECT rolname,rolcanlogin OR rolsuper OR rolcreatedb OR rolcreaterole
            OR rolreplication OR rolbypassrls AS unsafe
        FROM pg_roles WHERE rolname IN ('nexus_identity','nexus_catalog','nexus_billing',
            'nexus_publication','nexus_bridge','nexus_gateway')
    """)
        .all()
    )
    if len(roles) != 6 or any(row.unsafe for row in roles):
        raise RuntimeError(
            f"Provision restricted module, bridge and gateway roles before migration {revision}"
        )
    run_file("schema.sql")
    for schema, tables in SOURCES.items():
        op.execute(
            f"CREATE TABLE {schema}.source_revision("
            "id integer PRIMARY KEY CHECK(id=1), revision bigint NOT NULL)"
        )
        op.execute(f"INSERT INTO {schema}.source_revision VALUES(1,1)")
        op.execute(f"GRANT SELECT,UPDATE ON {schema}.source_revision TO nexus_{schema}")
        op.execute(f"""CREATE FUNCTION {schema}.advance_revision() RETURNS trigger
            LANGUAGE plpgsql SET search_path=pg_catalog AS $$ BEGIN
            UPDATE {schema}.source_revision SET revision=revision+1 WHERE id=1;
            RETURN NULL; END $$""")
        for table in tables:
            op.execute(
                "CREATE TRIGGER source_revision AFTER INSERT OR UPDATE OR DELETE "
                f"ON {schema}.{table} "
                f"FOR EACH STATEMENT EXECUTE FUNCTION {schema}.advance_revision()"
            )
    for name in ("admission.sql", "contracts.sql", "events.sql", "bridge.sql"):
        run_file(name)
    # Run business contracts with module privileges. The bridge owns no tables.
    for schema, owner, grantee in [
        ("billing", "nexus_billing", "nexus_bridge"),
        ("identity", "nexus_identity", "nexus_bridge"),
        ("catalog", "nexus_catalog", "nexus_bridge"),
        ("publication", "nexus_publication", "nexus_bridge"),
        ("gateway_v1", "nexus_bridge", "nexus_gateway"),
    ]:
        op.execute(f"GRANT USAGE ON SCHEMA {schema} TO {grantee}")
        op.execute(f"GRANT USAGE,CREATE ON SCHEMA {schema} TO {owner}")
        functions = signatures(schema)
        for signature in functions:
            op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
            op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO {grantee}")
            op.execute(f"ALTER FUNCTION {signature} OWNER TO {owner}")
        op.execute(f"REVOKE CREATE ON SCHEMA {schema} FROM {owner}")


def downgrade():
    op.execute("""DO $$ BEGIN IF EXISTS(SELECT FROM billing.request_runs WHERE node_id IS NOT NULL)
        THEN RAISE EXCEPTION 'gateway requests are retained'; END IF; END $$""")
    for schema in ("gateway_v1", "billing", "catalog", "identity", "publication"):
        functions = signatures(schema)
        owner = "nexus_bridge" if schema == "gateway_v1" else f"nexus_{schema}"
        op.execute(f"SET LOCAL ROLE {owner}")
        for signature in functions:
            op.execute(f"DROP FUNCTION IF EXISTS {signature}")
        op.execute("RESET ROLE")
    for schema, tables in SOURCES.items():
        for table in tables:
            op.execute(f"DROP TRIGGER source_revision ON {schema}.{table}")
        op.execute(f"DROP FUNCTION {schema}.advance_revision()")
        op.execute(f"DROP TABLE {schema}.source_revision")
    for table in ("usage_jobs", "normalized_usage", "usage_events"):
        op.execute(f"DROP TABLE billing.{table}")
    op.execute("DROP TRIGGER gateway_request_origin ON billing.request_runs")
    op.execute("DROP FUNCTION billing.protect_gateway_binding()")
    op.execute("ALTER TABLE billing.request_attempts DROP COLUMN sequence")
    op.execute(
        "ALTER TABLE billing.request_runs DROP COLUMN node_id, DROP COLUMN "
        "cluster_id, DROP COLUMN config_revision"
    )
    for table in (
        "limit_namespaces",
        "release_commands",
        "node_config_acks",
        "credential_envelopes",
        "config_outbox",
        "config_snapshots",
        "gateway_principals",
        "previous_keys",
        "gateway_clusters",
    ):
        op.execute(f"DROP TABLE publication.{table}")
    op.execute("DROP TABLE catalog.quota_policies,catalog.route_policies")
