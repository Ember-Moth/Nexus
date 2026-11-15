"""Keep container session holds ahead of the supplier minutes they may be billed."""

from pathlib import Path

from alembic import op

revision = "0032_container_coverage"
down_revision = "0031_model_call_bounds"
branch_labels = None
depends_on = None
SQL = Path(__file__).resolve().parents[1] / "sql"
D = SQL / revision
HOLD = (
    "v1_container_hold(uuid,uuid,jsonb,uuid,uuid,uuid,jsonb,text,text,integer,uuid,uuid,bigint,"
    "boolean,uuid)"
)
LINK = "v1_container_link(uuid,uuid,uuid,uuid,text,integer,boolean,text,boolean)"
NEW = [
    "v1_container_minutes(text,interval)",
    "v1_container_reach(uuid)",
    "v1_container_cover(uuid,uuid,timestamptz,jsonb,integer)",
    "v1_container_settle_cover(uuid,uuid,timestamptz,integer)",
]


def install(sql):
    op.execute("GRANT CREATE ON SCHEMA billing TO nexus_billing")
    op.execute("SET LOCAL ROLE nexus_billing")
    op.get_bind().exec_driver_sql(sql, execution_options={"no_parameters": True})
    op.execute("RESET ROLE")
    op.execute("REVOKE CREATE ON SCHEMA billing FROM nexus_billing")


def original(name):
    sql = (SQL / "0028_container_sessions" / "billing.sql").read_text()
    a = sql.index(f"FUNCTION {name}(")
    return "CREATE OR REPLACE " + sql[a : sql.index("END $$;", a) + len("END $$;")]


def upgrade():
    code = (D / "schema.sql").read_text() + "\n" + (D / "billing.sql").read_text()
    for name in NEW:
        code += f"\nREVOKE ALL ON FUNCTION billing.{name} FROM PUBLIC;"
    op.execute("GRANT REFERENCES ON billing.request_runs TO nexus_billing")
    install(code)
    op.execute("REVOKE REFERENCES ON billing.request_runs FROM nexus_billing")


def downgrade():
    op.execute("SET LOCAL ROLE nexus_billing")
    op.execute("""DO $$ BEGIN IF EXISTS(SELECT FROM billing.container_extensions) THEN
        RAISE EXCEPTION 'container extension holds are retained'; END IF; END $$""")
    op.execute("RESET ROLE")
    install(
        "\n".join(
            original(name)
            for name in (
                "billing.v1_container_hold",
                "billing.v1_container_link",
                "billing.v1_review_container",
            )
        )
        + "\n"
        + "\n".join(f"DROP FUNCTION billing.{name};" for name in reversed(NEW))
        + "\nDROP TABLE billing.container_extensions;"
        + "\nALTER TABLE billing.container_sessions DROP COLUMN covered_minutes,"
        " DROP COLUMN potential_end, DROP COLUMN idle_minutes;"
    )
