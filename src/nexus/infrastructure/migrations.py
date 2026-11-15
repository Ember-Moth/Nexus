"""Migration operations for tables that serve traffic while the schema changes.

A plain ``CREATE INDEX`` holds a SHARE lock on its table until the revision commits, which blocks
every insert and update for the whole build. Build indexes on busy tables with these helpers
instead::

    from nexus.infrastructure.migrations import create_index_concurrently

    def upgrade():
        create_index_concurrently(
            "audit_actor_idx", "billing.audit_events", "(actor_id)", where="actor_id IS NOT NULL"
        )

Each call commits the revision's work so far, runs in autocommit (``CONCURRENTLY`` cannot run in a
transaction block) and opens a new transaction for the statements that follow. Keep other DDL of
the same revision before the call, or in a separate revision, so that a failure cannot leave half
of a revision applied. A build interrupted by a failure leaves an INVALID index; calling the
helper again drops it and starts over.

Use the helpers in ``upgrade()`` only. Upgrades commit revision by revision; a downgrade runs in a
single transaction so that a revision refusing to drop retained data undoes the whole request,
and an autocommit block there would commit the revisions already downgraded.
"""

import re

from alembic import op
from sqlalchemy import text

IDENTIFIER = re.compile(r"[a-z_][a-z0-9_]*")


def _qualified(value: str) -> tuple[str, str]:
    schema, _, name = value.partition(".")
    if not (IDENTIFIER.fullmatch(schema) and IDENTIFIER.fullmatch(name)):
        raise ValueError(f"expected schema.name, got {value!r}")
    return schema, name


def _index_state(bind, schema: str, name: str) -> bool | None:
    """True for a valid index, False for an interrupted build, None when absent."""
    return bind.execute(
        text(
            "SELECT i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid "
            "JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname=:schema AND c.relname=:name"
        ),
        {"schema": schema, "name": name},
    ).scalar_one_or_none()


def _unbounded(bind, statement: str) -> None:
    # A concurrent build scans the table twice and waits, through the lock manager, for every
    # older transaction in the database. Its SHARE UPDATE EXCLUSIVE lock does not conflict with
    # reads or writes, so neither waiting nor running blocks traffic: the migration-wide
    # timeouts, which exist to protect traffic, would only abort it.
    names = ("lock_timeout", "statement_timeout")
    previous = {
        name: bind.execute(text("SELECT current_setting(:name)"), {"name": name}).scalar_one()
        for name in names
    }
    for name in names:
        bind.execute(text("SELECT set_config(:name, '0', false)"), {"name": name})
    try:
        bind.exec_driver_sql(statement)
    finally:
        for name, value in previous.items():
            bind.execute(
                text("SELECT set_config(:name, :value, false)"), {"name": name, "value": value}
            )


def create_index_concurrently(
    name: str, table: str, definition: str, *, where: str | None = None, unique: bool = False
) -> None:
    """Build ``schema.name`` on ``table`` without blocking writes; idempotent.

    ``definition`` is the parenthesized column or expression list, for example ``"(trace_id)"``.
    """
    schema, relation = _qualified(table)
    if not IDENTIFIER.fullmatch(name):
        raise ValueError(f"invalid index name {name!r}")
    statement = (
        f"CREATE {'UNIQUE ' if unique else ''}INDEX CONCURRENTLY {name} "
        f"ON {schema}.{relation} {definition}" + (f" WHERE {where}" if where else "")
    )
    with op.get_context().autocommit_block():
        bind = op.get_bind()
        state = _index_state(bind, schema, name)
        if state:
            return
        if state is False:
            _unbounded(bind, f"DROP INDEX CONCURRENTLY {schema}.{name}")
        _unbounded(bind, statement)


def drop_index_concurrently(index: str) -> None:
    """Drop ``schema.name`` without blocking reads or writes; idempotent."""
    schema, name = _qualified(index)
    with op.get_context().autocommit_block():
        _unbounded(op.get_bind(), f"DROP INDEX CONCURRENTLY IF EXISTS {schema}.{name}")
