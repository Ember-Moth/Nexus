"""Tenant-owned native Message Batches and per-item settlement."""

from pathlib import Path

from alembic import op

revision = "0022_message_batches"
down_revision = "0021_background_responses"
branch_labels = None
depends_on = None
SQL = Path(__file__).resolve().parents[1] / "sql" / revision
CONTRACTS = [
    (
        "billing",
        "nexus_billing",
        "nexus_bridge",
        "billing.sql",
        [
            "v1_batch_pricing(uuid,uuid)",
            "v1_reserve_item(uuid,uuid,uuid,uuid,uuid,uuid,bigint,bigint,integer,text,jsonb,timestamptz,uuid,uuid,bigint,boolean)",
        ],
    ),
    (
        "publication",
        "nexus_publication",
        "nexus_bridge",
        "resources.sql",
        [
            "v1_batch_prepare(uuid,uuid,bigint,jsonb,jsonb,jsonb)",
            "v1_batch_update(uuid,text,jsonb,boolean)",
            "v1_batch_lookup(uuid,uuid)",
            "v1_batch_page(uuid,uuid,boolean,integer)",
            "v1_batch_claim()",
            "v1_batch_lock(uuid)",
            "v1_batch_items(uuid)",
            "v1_batch_done(uuid)",
        ],
    ),
    (
        "gateway_v1",
        "nexus_bridge",
        "nexus_gateway",
        "gateway.sql",
        [
            "batch_prepare(uuid,uuid,bigint,uuid,jsonb,jsonb)",
            "batch_resolve(uuid,uuid,bigint)",
            "batch_page(uuid,bigint,uuid,boolean,integer)",
            "batch_record(uuid,uuid,bigint,text,jsonb,boolean)",
            "batch_claim()",
            "batch_finish(uuid,text,jsonb,jsonb,text)",
        ],
    ),
]


def upgrade():
    op.execute("""
        CREATE FUNCTION billing.protect_batch_attempt() RETURNS trigger
        LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$ BEGIN
        IF EXISTS(SELECT FROM billing.request_runs WHERE id=NEW.request_id
                  AND billing_policy_version='anthropic_message_batch_v1') THEN
            RAISE EXCEPTION 'invalid_attempt_sequence';
        END IF;
        RETURN NEW;
        END $$
    """)
    op.execute("REVOKE ALL ON FUNCTION billing.protect_batch_attempt() FROM PUBLIC")
    op.execute("""
        CREATE TRIGGER batch_attempt BEFORE INSERT ON billing.request_attempts FOR EACH ROW
        WHEN (NEW.sequence IS DISTINCT FROM 1) EXECUTE FUNCTION billing.protect_batch_attempt()
    """)
    for schema, owner, grantee, file, names in CONTRACTS:
        op.execute(f"GRANT CREATE ON SCHEMA {schema} TO {owner}")
        op.execute(f"SET LOCAL ROLE {owner}")
        op.get_bind().exec_driver_sql(
            (SQL / file).read_text(), execution_options={"no_parameters": True}
        )
        for name in names:
            op.execute(f"REVOKE ALL ON FUNCTION {schema}.{name} FROM PUBLIC")
            op.execute(f"GRANT EXECUTE ON FUNCTION {schema}.{name} TO {grantee}")
        op.execute("RESET ROLE")
        op.execute(f"REVOKE CREATE ON SCHEMA {schema} FROM {owner}")


def downgrade():
    op.execute("SET LOCAL ROLE nexus_publication")
    op.execute("""DO $$ BEGIN IF EXISTS(SELECT FROM publication.message_batches
        WHERE NOT done OR (provider_id IS NOT NULL AND NOT deleted))
               THEN RAISE EXCEPTION 'message batches remain active'; END IF; END $$""")
    op.execute("RESET ROLE")
    for schema, owner, file, name in [
        ("gateway_v1", "nexus_bridge", "0008_gateway_protocol/bridge.sql", "reserve_request"),
        ("billing", "nexus_billing", "0012_gateway_admission/admission.sql", "v1_reserve"),
    ]:
        source = (SQL.parent / file).read_text()
        start = source.index(f"FUNCTION {schema}.{name}(")
        end = source.index("END $$;", start) + len("END $$;")
        op.execute(f"GRANT CREATE ON SCHEMA {schema} TO {owner}")
        op.execute(f"SET LOCAL ROLE {owner}")
        op.get_bind().exec_driver_sql(
            "CREATE OR REPLACE " + source[start:end], execution_options={"no_parameters": True}
        )
        if schema == "gateway_v1":
            op.execute(
                "DROP FUNCTION gateway_v1.reserve_item(uuid,uuid,uuid,bigint,bigint,bigint,boolean)"
            )
        op.execute("RESET ROLE")
        op.execute(f"REVOKE CREATE ON SCHEMA {schema} FROM {owner}")
    for schema, owner, _, _, names in reversed(CONTRACTS):
        op.execute(f"SET LOCAL ROLE {owner}")
        for name in reversed(names):
            op.execute(f"DROP FUNCTION {schema}.{name}")
        op.execute("RESET ROLE")
    op.execute("SET LOCAL ROLE nexus_publication")
    op.execute("DROP TABLE publication.batch_items, publication.message_batches")
    op.execute("RESET ROLE")
    op.execute("DROP TRIGGER batch_attempt ON billing.request_attempts")
    op.execute("DROP FUNCTION billing.protect_batch_attempt()")
