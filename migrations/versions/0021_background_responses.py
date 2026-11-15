"""Recoverable polling and atomic settlement of background Responses."""

from pathlib import Path

from alembic import op

revision = "0021_background_responses"
down_revision = "0020_tenant_files"
branch_labels = None
depends_on = None
SQL = Path(__file__).resolve().parents[1] / "sql" / revision
CONTRACTS = [
    (
        "publication",
        "nexus_publication",
        "nexus_bridge",
        "jobs.sql",
        [
            "v1_background_register(uuid,uuid,uuid,bigint,uuid,uuid)",
            "v1_background_claim()",
            "v1_job(uuid)",
            "v1_job_done(uuid)",
            "v1_job_pending(uuid)",
        ],
    ),
    (
        "gateway_v1",
        "nexus_bridge",
        "nexus_gateway",
        "gateway.sql",
        [
            "background_register(uuid,uuid,uuid,text,uuid,uuid)",
            "background_claim()",
            "background_finish(uuid,text,text,jsonb)",
            "response_can_delete(uuid,uuid,bigint)",
        ],
    ),
]


def upgrade():
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
    op.execute("""DO $$ BEGIN IF EXISTS(SELECT FROM publication.response_jobs
        WHERE NOT done)
               THEN RAISE EXCEPTION 'background responses are pending'; END IF; END $$""")
    op.execute("RESET ROLE")
    for schema, owner, _, _, names in reversed(CONTRACTS):
        op.execute(f"SET LOCAL ROLE {owner}")
        for name in reversed(names):
            op.execute(f"DROP FUNCTION {schema}.{name}")
        op.execute("RESET ROLE")
    op.execute("SET LOCAL ROLE nexus_publication")
    op.execute("DROP TABLE publication.response_jobs")
    op.execute("RESET ROLE")
