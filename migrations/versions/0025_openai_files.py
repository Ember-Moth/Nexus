"""Add native OpenAI Files to the tenant file registry."""

from pathlib import Path

from alembic import op

revision = "0025_openai_files"
down_revision = "0024_web_search"
branch_labels = None
depends_on = None
SQL = Path(__file__).resolve().parents[1] / "sql" / revision
CONTRACTS = [
    (
        "publication",
        "nexus_publication",
        "nexus_bridge",
        "files.sql",
        [
            "v1_openai_file_id(uuid,uuid,text)",
            "v1_native_file(uuid,uuid,text)",
            "v1_native_file_save(uuid,uuid,uuid,jsonb,jsonb,text)",
            "v1_native_files(uuid,uuid,boolean,integer,text,boolean,text)",
        ],
    ),
    (
        "gateway_v1",
        "nexus_bridge",
        "nexus_gateway",
        "gateway.sql",
        [
            "native_files(uuid,bigint,uuid,boolean,integer,text,boolean,text)",
            "native_file_save(uuid,uuid,bigint,uuid,uuid,jsonb,text)",
            "native_file_resolve(uuid,uuid,bigint,text)",
            "native_file_remove(uuid,uuid,bigint,text)",
            "openai_file_id(uuid,bigint,uuid,uuid,text)",
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
    op.execute("""DO $$ BEGIN IF EXISTS(SELECT FROM publication.tenant_files
        WHERE endpoint='openai_responses' AND deleted_at IS NULL
        AND (expires_at IS NULL OR expires_at>clock_timestamp()))
               THEN RAISE EXCEPTION 'files are retained'; END IF; END $$""")
    op.execute("RESET ROLE")
    op.execute("GRANT CREATE ON SCHEMA publication TO nexus_publication")
    op.execute("SET LOCAL ROLE nexus_publication")
    previous = (SQL.parent / "0020_tenant_files" / "files.sql").read_text()
    for name in ["v1_file", "v1_file_save", "v1_files"]:
        start = previous.index(f"FUNCTION publication.{name}(")
        end = previous.index("$$;", start) + 3
        op.get_bind().exec_driver_sql(
            "CREATE OR REPLACE " + previous[start:end], execution_options={"no_parameters": True}
        )
    op.execute("RESET ROLE")
    op.execute("REVOKE CREATE ON SCHEMA publication FROM nexus_publication")
    for schema, owner, _, _, names in reversed(CONTRACTS):
        op.execute(f"SET LOCAL ROLE {owner}")
        for name in reversed(names):
            op.execute(f"DROP FUNCTION {schema}.{name}")
        op.execute("RESET ROLE")
    op.execute("SET LOCAL ROLE nexus_publication")
    op.execute("DELETE FROM publication.tenant_files WHERE endpoint='openai_responses'")
    op.execute(
        "DROP INDEX publication.tenant_files_protocol_page, publication.tenant_files_upstream_id"
    )
    op.execute("ALTER TABLE publication.tenant_files DROP COLUMN endpoint,DROP COLUMN expires_at")
    op.execute("RESET ROLE")
