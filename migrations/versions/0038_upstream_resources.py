"""Let organizations list the upstream copies their keys hold and request their deletion.

- ``publication.upstream_resources`` lists every copy bound to a key. The automatic schedule
  now reads it too, so owner requests and the grace-period purge select the same copies.
- ``publication.purge_requests`` records each owner request (idempotency key, actor, scope);
  ``purge_request_items`` links it to the ``upstream_purges`` rows it covers. Requested purges
  use the reason ``owner_request`` and run through the existing node purge loop.
- Stored responses and conversations gain ``created_at``; every resource table is indexed by key
  and creation time for the listing.
"""

from pathlib import Path

from alembic import op

revision = "0038_upstream_resources"
down_revision = "0037_auto_publish"
branch_labels = None
depends_on = None
SQL = Path(__file__).resolve().parents[1] / "sql" / revision


def upgrade():
    op.execute("GRANT CREATE ON SCHEMA publication TO nexus_publication")
    op.execute("SET LOCAL ROLE nexus_publication")
    op.get_bind().exec_driver_sql(
        (SQL / "resources.sql").read_text(), execution_options={"no_parameters": True}
    )
    op.execute("RESET ROLE")
    op.execute("REVOKE CREATE ON SCHEMA publication FROM nexus_publication")


def downgrade():
    op.execute("GRANT CREATE ON SCHEMA publication TO nexus_publication")
    op.execute("SET LOCAL ROLE nexus_publication")
    op.execute("""DO $$ BEGIN IF EXISTS(SELECT FROM publication.upstream_purges
        WHERE reason='owner_request') THEN
        RAISE EXCEPTION 'owner-requested upstream purges are retained'; END IF; END $$""")
    op.get_bind().exec_driver_sql(
        (SQL / "downgrade.sql").read_text(), execution_options={"no_parameters": True}
    )
    for table in (
        "message_batches",
        "execution_containers",
        "vector_stores",
        "tenant_files",
    ):
        op.execute(f"DROP INDEX publication.{table}_key_created")
    op.execute("DROP INDEX publication.upstream_purge_key")
    op.execute("DROP INDEX publication.upstream_purge_resource")
    op.execute("DROP TABLE publication.purge_request_items")
    op.execute("DROP TABLE publication.purge_requests")
    op.execute("DROP VIEW publication.upstream_resources")
    op.execute("""ALTER TABLE publication.upstream_purges
        DROP CONSTRAINT upstream_purges_reason_check,
        ADD CONSTRAINT upstream_purges_reason_check CHECK(reason IN ('key_inactive','orphan'))""")
    # Dropping created_at also drops the two tables' key indexes.
    op.execute("ALTER TABLE publication.conversations DROP COLUMN created_at")
    op.execute("ALTER TABLE publication.stored_responses DROP COLUMN created_at")
    op.execute("RESET ROLE")
    op.execute("REVOKE CREATE ON SCHEMA publication FROM nexus_publication")
