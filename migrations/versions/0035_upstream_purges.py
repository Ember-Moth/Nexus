"""Purge upstream copies of tenant resources after their key becomes unusable."""

from pathlib import Path

from alembic import op

revision = "0035_upstream_purges"
down_revision = "0034_resource_quotas"
branch_labels = None
depends_on = None
SQL = Path(__file__).resolve().parents[1] / "sql" / revision
CONTRACTS = [
    ("identity", "nexus_identity", "nexus_bridge", "identity.sql", ["v1_key_activity(uuid[])"]),
    (
        "publication",
        "nexus_publication",
        "nexus_bridge",
        "purges.sql",
        [
            "v1_purge_owners(uuid)",
            "v1_purge_schedule(uuid,uuid[],jsonb,interval)",
            "v1_purge_claim(uuid,uuid,integer)",
            "v1_purge_finish(uuid,uuid,uuid,text,text)",
            "v1_purge_orphan(uuid,uuid,text,jsonb,text,text)",
        ],
    ),
    (
        "gateway_v1",
        "nexus_bridge",
        "nexus_gateway",
        "gateway.sql",
        [
            "claim_purges(integer,integer)",
            "finish_purge(uuid,text,text)",
            "record_orphan(uuid,bigint,text,uuid,text,text)",
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
    op.execute("""DO $$ BEGIN IF EXISTS(SELECT FROM publication.upstream_purges
        WHERE state='pending') THEN RAISE EXCEPTION 'upstream purges remain pending';
        END IF; END $$""")
    op.execute("RESET ROLE")
    for schema, owner, _, _, names in reversed(CONTRACTS):
        op.execute(f"SET LOCAL ROLE {owner}")
        for name in reversed(names):
            op.execute(f"DROP FUNCTION {schema}.{name}")
        op.execute("RESET ROLE")
    op.execute("SET LOCAL ROLE nexus_publication")
    op.execute("DROP TABLE publication.upstream_purges")
    op.execute("DROP TABLE publication.inactive_keys")
    op.execute("RESET ROLE")
