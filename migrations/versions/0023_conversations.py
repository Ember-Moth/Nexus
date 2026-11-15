"""Tenant-owned OpenAI conversation bindings."""

from pathlib import Path

from alembic import op

revision = "0023_conversations"
down_revision = "0022_message_batches"
branch_labels = None
depends_on = None
SQL = Path(__file__).resolve().parents[1] / "sql" / revision
CONTRACTS = [
    (
        "publication",
        "nexus_publication",
        "nexus_bridge",
        "resources.sql",
        [
            "v1_conversation(uuid,uuid)",
            "v1_conversation_id(uuid,uuid,text)",
            "v1_conversation_save(uuid,uuid,jsonb,text)",
            "v1_conversation_remove(uuid,uuid)",
        ],
    ),
    (
        "gateway_v1",
        "nexus_bridge",
        "nexus_gateway",
        "gateway.sql",
        [
            "conversation_save(uuid,uuid,bigint,uuid,uuid,text)",
            "conversation_resolve(uuid,uuid,bigint)",
            "conversation_id(uuid,bigint,uuid,uuid,text)",
            "conversation_remove(uuid,uuid,bigint)",
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
    op.execute("""DO $$ BEGIN IF EXISTS(SELECT FROM publication.conversations
        WHERE deleted_at IS NULL)
               THEN RAISE EXCEPTION 'conversations remain active'; END IF; END $$""")
    op.execute("RESET ROLE")
    for schema, owner, _, _, names in reversed(CONTRACTS):
        op.execute(f"SET LOCAL ROLE {owner}")
        for name in reversed(names):
            op.execute(f"DROP FUNCTION {schema}.{name}")
        op.execute("RESET ROLE")
    op.execute("SET LOCAL ROLE nexus_publication")
    op.execute("DROP TABLE publication.conversations")
    op.execute("RESET ROLE")
