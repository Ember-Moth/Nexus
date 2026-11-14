"""Pause new gateway reservations when ready settlement jobs exceed the operating limit."""

from alembic import op

revision = "0010_settlement_backpressure"
down_revision = "0009_operations"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        ALTER TABLE billing.settings ADD COLUMN settlement_backlog_limit integer
        NOT NULL DEFAULT 1024 CHECK (settlement_backlog_limit > 0)
    """)
    op.execute("""
        CREATE FUNCTION billing.check_settlement_backlog() RETURNS trigger
        LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$
        DECLARE threshold integer;
        BEGIN
            SELECT settlement_backlog_limit INTO STRICT threshold FROM billing.settings WHERE id=1;
            IF EXISTS (SELECT FROM billing.usage_jobs WHERE state='ready'
                       LIMIT 1 OFFSET (threshold-1)) THEN
                RAISE EXCEPTION 'settlement_backlog';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER settlement_backpressure BEFORE INSERT ON billing.request_runs
        FOR EACH ROW WHEN (NEW.node_id IS NOT NULL)
        EXECUTE FUNCTION billing.check_settlement_backlog()
    """)


def downgrade():
    op.execute("DROP TRIGGER settlement_backpressure ON billing.request_runs")
    op.execute("DROP FUNCTION billing.check_settlement_backlog()")
    op.execute("ALTER TABLE billing.settings DROP COLUMN settlement_backlog_limit")
