"""Drop gateway functions no Relay calls any more.

- ``gateway_v1.file_save``, ``file_resolve``, ``file_remove`` and ``files`` with their
  ``publication.v1_file*`` helpers: the Messages-only file calls; the Relay uses the
  ``native_file*`` functions for both endpoints.
- ``gateway_v1.container_remove``: the Relay reports removed containers through
  ``container_retired``.
"""

from pathlib import Path

from alembic import op

revision = "0048_superseded_gateway"
down_revision = "0047_builtin_admin"
branch_labels = None
depends_on = None

SQL = Path(__file__).resolve().parents[1] / "sql" / revision
GATEWAY = (
    "gateway_v1.file_save(uuid,uuid,bigint,uuid,uuid,jsonb)",
    "gateway_v1.file_remove(uuid,uuid,bigint)",
    "gateway_v1.file_resolve(uuid,uuid,bigint)",
    "gateway_v1.files(uuid,bigint,uuid,boolean,integer)",
    "gateway_v1.container_remove(uuid,uuid,bigint)",
)
PUBLICATION = (
    "publication.v1_file_save(uuid,uuid,uuid,jsonb,jsonb)",
    "publication.v1_file(uuid,uuid)",
    "publication.v1_files(uuid,uuid,boolean,integer)",
)


def upgrade():
    op.execute("SET LOCAL ROLE nexus_bridge")
    op.execute(f"DROP FUNCTION {','.join(GATEWAY)}")
    op.execute("SET LOCAL ROLE nexus_publication")
    op.execute(f"DROP FUNCTION {','.join(PUBLICATION)}")
    op.execute("RESET ROLE")


def downgrade():
    for schema, owner, file, names, caller in (
        ("publication", "nexus_publication", "publication.sql", PUBLICATION, "nexus_bridge"),
        ("gateway_v1", "nexus_bridge", "gateway.sql", GATEWAY, "nexus_gateway"),
    ):
        op.execute(f"GRANT CREATE ON SCHEMA {schema} TO {owner}")
        op.execute(f"SET LOCAL ROLE {owner}")
        sql = (SQL / file).read_text()
        op.get_bind().exec_driver_sql(sql, execution_options={"no_parameters": True})
        for name in names:
            op.execute(f"REVOKE ALL ON FUNCTION {name} FROM PUBLIC")
            op.execute(f"GRANT EXECUTE ON FUNCTION {name} TO {caller}")
        op.execute("RESET ROLE")
        op.execute(f"REVOKE CREATE ON SCHEMA {schema} FROM {owner}")
