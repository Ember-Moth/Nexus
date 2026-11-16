"""Baseline: the complete schema of an empty installation.

The DDL lives in ``migrations/sql/0001_baseline`` and runs in file-name order: schemas, the tables
of each module with their keys, checks and indexes, foreign keys, the starting rows, functions,
views, triggers, ownership, privileges and finally the built-in administrator.

Roles are cluster-wide and come from provisioning (``nexus provision-database``, or
``scripts/native_services.py`` locally), never from a migration. ``nexus_meta`` and its
``alembic_version`` table belong to ``migrations/env.py``.

Revisions after this one follow the expand/contract rules in ``docs/operations.md``.
"""

from pathlib import Path

from alembic import op

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None

SQL = Path(__file__).resolve().parents[1] / "sql" / revision
SCHEMAS = ("gateway_v1", "reporting", "publication", "billing", "catalog", "identity")


def upgrade():
    # Functions are created before some of the functions and tables their bodies use.
    op.execute("SET LOCAL check_function_bodies = off")
    bind = op.get_bind()
    for path in sorted(SQL.glob("*.sql")):
        bind.exec_driver_sql(path.read_text(), execution_options={"no_parameters": True})


def downgrade():
    """Drop every object of the baseline, and with it all data; the roles stay."""
    op.execute(f"DROP SCHEMA {', '.join(SCHEMAS)} CASCADE")
    op.execute(
        "ALTER DEFAULT PRIVILEGES FOR ROLE nexus_migrator GRANT EXECUTE ON FUNCTIONS TO PUBLIC"
    )
