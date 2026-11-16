"""Export files live in object storage; ``reporting.export_jobs`` keeps their keys.

- ``content`` is gone. The Worker uploads each attempt's CSV to
  ``exports/{organization_id}/{export_id}/{fence}.csv`` before its fenced commit, and the job
  records the winning attempt's ``object_key`` and ``size_bytes``. A ready job always has both.
- ``purge_at`` schedules the deletion of every object under the job's prefix once it has
  expired. While a deletion runs it holds the row as a lease; a failure leaves the lease to lapse
  and the deletion is retried.
"""

from alembic import op

revision = "0046_export_objects"
down_revision = "0045_indexed_admission"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""ALTER TABLE reporting.export_jobs DROP COLUMN content,
        ADD COLUMN object_key text CHECK(object_key ~ '^exports/'),
        ADD COLUMN size_bytes bigint CHECK(size_bytes >= 0),
        ADD COLUMN purge_at timestamptz,
        ADD CONSTRAINT export_ready_object
            CHECK(state<>'ready' OR (object_key IS NOT NULL AND size_bytes IS NOT NULL))""")
    op.execute(
        "CREATE INDEX export_purge ON reporting.export_jobs(purge_at) WHERE purge_at IS NOT NULL"
    )


def downgrade():
    # The files stay in object storage; their jobs read as expired.
    op.execute("UPDATE reporting.export_jobs SET state='expired' WHERE state='ready'")
    op.execute("""ALTER TABLE reporting.export_jobs DROP COLUMN object_key,
        DROP COLUMN size_bytes, DROP COLUMN purge_at, ADD COLUMN content bytea""")
