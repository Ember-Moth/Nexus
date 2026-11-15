"""Prepay vector storage ahead of use and retire stores whose renewal lapsed."""

from pathlib import Path

from alembic import op

revision = "0033_storage_prepayment"
down_revision = "0032_container_coverage"
branch_labels = None
depends_on = None
SQL = Path(__file__).resolve().parents[1] / "sql"
D = SQL / revision
PRIOR = SQL / "0026_vector_stores"
PRIVATE = [
    "v1_storage_release(uuid,text)",
    "v1_storage_hold(uuid,timestamptz,bigint)",
    "v1_storage_restore(uuid)",
    "v1_renew_storage(integer,text)",
]


def install(schema, owner, sql):
    op.execute(f"GRANT CREATE ON SCHEMA {schema} TO {owner}")
    op.execute(f"SET LOCAL ROLE {owner}")
    op.get_bind().exec_driver_sql(sql, execution_options={"no_parameters": True})
    op.execute("RESET ROLE")
    op.execute(f"REVOKE CREATE ON SCHEMA {schema} FROM {owner}")


def original(file, name, end="END $$;"):
    sql = (PRIOR / file).read_text()
    a = sql.index(f"FUNCTION {name}(")
    return "CREATE OR REPLACE " + sql[a : sql.index(end, a) + len(end)]


def upgrade():
    code = (D / "schema.sql").read_text() + "\n" + (D / "storage.sql").read_text()
    for name in PRIVATE:
        code += f"\nREVOKE ALL ON FUNCTION billing.{name} FROM PUBLIC;"
    code += "\nREVOKE ALL ON FUNCTION billing.v1_storage_lapsed(uuid) FROM PUBLIC;"
    code += "\nGRANT EXECUTE ON FUNCTION billing.v1_storage_lapsed(uuid) TO nexus_bridge;"
    op.execute("GRANT REFERENCES ON billing.request_runs TO nexus_billing")
    install("billing", "nexus_billing", code)
    op.execute("REVOKE REFERENCES ON billing.request_runs FROM nexus_billing")
    install("gateway_v1", "nexus_bridge", (D / "gateway.sql").read_text())


def downgrade():
    op.execute("SET LOCAL ROLE nexus_billing")
    op.execute("""DO $$ BEGIN IF EXISTS(SELECT FROM billing.vector_storage_contracts
        WHERE hold_id IS NOT NULL OR lapsed_at IS NOT NULL) THEN
        RAISE EXCEPTION 'prepaid storage holds are retained'; END IF; END $$""")
    op.execute("RESET ROLE")
    install(
        "gateway_v1",
        "nexus_bridge",
        original("gateway.sql", "gateway_v1.vector_binding")
        + "\n"
        + original("gateway.sql", "gateway_v1.vector_claim", "\n$$;"),
    )
    code = "\n".join(
        original("storage.sql", name)
        for name in (
            "billing.v1_storage_sample",
            "billing.v1_stage_storage",
        )
    )
    code += "\n" + original("storage.sql", "billing.v1_storage_write", "\n$$;")
    code += "\n" + "\n".join(
        f"DROP FUNCTION billing.{name};" for name in [*reversed(PRIVATE), "v1_storage_lapsed(uuid)"]
    )
    code += (
        "\nALTER TABLE billing.vector_storage_contracts DROP COLUMN hold_id,"
        " DROP COLUMN prepaid_until, DROP COLUMN prepaid_bytes, DROP COLUMN renew_after,"
        " DROP COLUMN renewal_failed_at, DROP COLUMN lapsed_at, DROP COLUMN lapse_action;"
    )
    install("billing", "nexus_billing", code)
