"""Accept input bounds that cover every model call of a looping request.

Server tool loops and compaction re-read the context. The relay now reserves the per-call
input ceiling once for each model call the request may bill, up to 64 calls. Each call
still stays within the deployment's input_ceiling.
"""

from alembic import op

revision = "0031_model_call_bounds"
down_revision = "0030_chat_upstream"
branch_labels = None
depends_on = None
SINGLE = "inputs NOT BETWEEN 0 AND m.max_input_tokens OR"
MULTIPLE = "inputs NOT BETWEEN 0 AND m.max_input_tokens::bigint*64 OR"


def patch(old, new):
    # Earlier revisions edit this function in place, so start from the installed body.
    sql = (
        op.get_bind()
        .exec_driver_sql(
            "SELECT pg_get_functiondef("
            "'catalog.v1_eligible(uuid,uuid,jsonb,uuid,bigint,bigint)'::regprocedure)"
        )
        .scalar_one()
    )
    if sql.count(old) != 1:
        raise ValueError("Unexpected catalog.v1_eligible body")
    op.execute("GRANT CREATE ON SCHEMA catalog TO nexus_catalog")
    op.execute("SET LOCAL ROLE nexus_catalog")
    op.get_bind().exec_driver_sql(sql.replace(old, new), execution_options={"no_parameters": True})
    op.execute("RESET ROLE")
    op.execute("REVOKE CREATE ON SCHEMA catalog FROM nexus_catalog")


def upgrade():
    patch(SINGLE, MULTIPLE)


def downgrade():
    # An open request whose bound spans several calls could no longer start an attempt.
    op.execute("""DO $$ BEGIN IF EXISTS (SELECT FROM billing.request_runs r
        JOIN catalog.models m ON m.id=r.model_id WHERE r.reservation_state='held'
        AND r.input_limit>m.max_input_tokens) THEN
        RAISE EXCEPTION 'multi-call reservations are open'; END IF; END $$""")
    patch(MULTIPLE, SINGLE)
