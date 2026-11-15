"""Settlement backpressure without scanning the queue inside an organization's admission.

- ``billing.settlement_backlog`` is a one-row gauge: the number of ready settlement jobs (counted
  up to the limit) and when it was counted.
- ``attempt.finished`` accepts the outcome ``declined``: the upstream provably did not execute
  the attempt (an admission rejection its vendor documents, or a request never sent). It must
  report no usage or only zeros; the Worker confirms its cost as zero instead of opening a
  ``missing_usage`` case. An attempt a node failed over from is settled the same way.
- ``billing.check_settlement_backlog`` still guards every node reservation, but it reads the
  gauge. The reservation that finds the gauge older than 100 ms recounts it, unless another
  reservation is already doing so (SKIP LOCKED), so admissions never wait for each other here.
  Before, every reservation counted up to ``settlement_backlog_limit`` ready jobs while holding
  its organization's admission lock. The gauge may trail the queue by the refresh interval;
  the limit was always approximate (docs/operations.md).
- ``settlement_backlog_limit`` defaults to 2048 instead of 1024: a Worker now settles one wallet
  at about 1,500 jobs per second, so a full backlog drains in one to two seconds
  (docs/relay-acceptance.md, "Worker 优化后").
"""

from alembic import op

revision = "0044_settlement_throughput"
down_revision = "0043_cluster_policy"
branch_labels = None
depends_on = None

CHECK = """
CREATE OR REPLACE FUNCTION billing.check_settlement_backlog() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$
DECLARE threshold integer; backlog integer; counted timestamptz;
BEGIN
    SELECT settlement_backlog_limit INTO STRICT threshold FROM billing.settings WHERE id=1;
    SELECT g.ready, g.observed_at INTO STRICT backlog, counted
    FROM billing.settlement_backlog g WHERE g.id=1;
    IF counted < clock_timestamp() - interval '100 milliseconds' THEN
        -- One reservation recounts; the others keep the last count instead of waiting.
        PERFORM FROM billing.settlement_backlog g WHERE g.id=1 FOR UPDATE SKIP LOCKED;
        IF FOUND THEN
            SELECT count(*) INTO backlog FROM (
                SELECT FROM billing.usage_jobs j WHERE j.state='ready' LIMIT threshold) due;
            UPDATE billing.settlement_backlog SET ready=backlog, observed_at=clock_timestamp()
            WHERE id=1;
        END IF;
    END IF;
    IF backlog >= threshold THEN
        RAISE EXCEPTION 'settlement_backlog';
    END IF;
    RETURN NEW;
END $$
"""

SCAN = """
CREATE OR REPLACE FUNCTION billing.check_settlement_backlog() RETURNS trigger
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
"""

OUTCOMES = "body->>'outcome' NOT IN ('succeeded','failed','cancelled','unknown')"
DECLINED = "body->>'outcome' NOT IN ('succeeded','failed','cancelled','unknown','declined')"
SHAPE = (
    "IF jsonb_typeof(raw) NOT IN ('object','null') THEN RAISE EXCEPTION 'invalid_usage_shape'; "
    "END IF;"
)
NO_USAGE = (
    SHAPE
    + "\n  -- A declined attempt was not executed: it reports no usage or only zeros."
    + "\n  IF body->>'outcome'='declined' AND jsonb_typeof(raw)='object' AND EXISTS("
    + "SELECT FROM jsonb_each(raw) WHERE value IS DISTINCT FROM '0'::jsonb)"
    + " THEN RAISE EXCEPTION 'invalid_attempt_fact'; END IF;"
)

LIMIT_DEFAULT = "ALTER TABLE billing.settings ALTER COLUMN settlement_backlog_limit SET DEFAULT"


def declined_outcome(enable):
    """Patch the installed usage intake, as 0028 does, to accept or refuse ``declined``."""
    bind = op.get_bind()
    sql = bind.exec_driver_sql(
        "SELECT pg_get_functiondef('billing.v1_append(text,uuid)'::regprocedure)"
    ).scalar_one()
    pairs = [(OUTCOMES, DECLINED), (SHAPE, NO_USAGE)]
    for old, new in pairs if enable else [(n, o) for o, n in reversed(pairs)]:
        if sql.count(old) != 1:
            raise ValueError("Unexpected usage envelope")
        sql = sql.replace(old, new)
    op.execute("GRANT CREATE ON SCHEMA billing TO nexus_billing")
    op.execute("SET LOCAL ROLE nexus_billing")
    bind.exec_driver_sql(sql, execution_options={"no_parameters": True})
    op.execute("RESET ROLE")
    op.execute("REVOKE CREATE ON SCHEMA billing FROM nexus_billing")


def upgrade():
    op.execute("""
        CREATE TABLE billing.settlement_backlog (
            id smallint PRIMARY KEY DEFAULT 1 CHECK (id = 1),
            ready integer NOT NULL DEFAULT 0 CHECK (ready >= 0),
            observed_at timestamptz NOT NULL DEFAULT '-infinity'
        )
    """)
    op.execute("INSERT INTO billing.settlement_backlog DEFAULT VALUES")
    # The trigger runs as the reservation functions' owner.
    op.execute("GRANT SELECT, UPDATE ON billing.settlement_backlog TO nexus_billing")
    op.execute(CHECK)
    declined_outcome(True)
    op.execute(f"{LIMIT_DEFAULT} 2048")
    op.execute(
        "UPDATE billing.settings SET settlement_backlog_limit=2048 "
        "WHERE id=1 AND settlement_backlog_limit=1024"
    )


def downgrade():
    op.execute(
        "UPDATE billing.settings SET settlement_backlog_limit=1024 "
        "WHERE id=1 AND settlement_backlog_limit=2048"
    )
    op.execute(f"{LIMIT_DEFAULT} 1024")
    declined_outcome(False)
    op.execute(SCAN)
    op.execute("DROP TABLE billing.settlement_backlog")
