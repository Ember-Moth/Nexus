"""Financial queries, reporting projections, exports and operational reconciliation."""

from pathlib import Path

from alembic import op

revision = "0009_operations"
down_revision = "0008_gateway_protocol"
branch_labels = None
depends_on = None
SQL = Path(__file__).resolve().parents[1] / "sql" / revision


def usage_replay_counter(enabled):
    original = (SQL.parent / "0008_gateway_protocol" / "events.sql").read_text()
    statement = original.replace(
        "CREATE FUNCTION billing.v1_append", "CREATE OR REPLACE FUNCTION billing.v1_append"
    )
    if enabled:
        statement = statement.replace(
            "IF FOUND AND old.envelope=e THEN RETURN",
            """IF FOUND AND old.envelope=e THEN
          INSERT INTO billing.ingest_counters(name,total) VALUES('usage',1)
          ON CONFLICT(name) DO UPDATE SET total=billing.ingest_counters.total+1;
          RETURN""",
        )
    op.execute("GRANT CREATE ON SCHEMA billing TO nexus_billing")
    op.execute("SET LOCAL ROLE nexus_billing")
    op.get_bind().exec_driver_sql(statement, execution_options={"no_parameters": True})
    op.execute("RESET ROLE")
    op.execute("REVOKE CREATE ON SCHEMA billing FROM nexus_billing")


def upgrade():
    for name in ("schema.sql", "facts.sql"):
        op.get_bind().exec_driver_sql(
            (SQL / name).read_text(), execution_options={"no_parameters": True}
        )

    usage_replay_counter(True)


def downgrade():
    op.execute("""DO $$ BEGIN IF EXISTS(SELECT FROM billing.case_decisions) OR EXISTS(SELECT FROM
billing.upstream_statements) THEN
      RAISE EXCEPTION 'reconciliation decisions are retained'; END IF; END $$""")
    op.execute("DROP TRIGGER new_review_evidence ON billing.usage_events")
    op.execute("DROP FUNCTION billing.bump_review_evidence()")
    op.execute("ALTER TABLE billing.payment_orders DROP COLUMN verified_observation_id")
    op.execute("ALTER TABLE billing.payment_observations DROP COLUMN query_elapsed_ms")
    op.execute("DROP TABLE billing.upstream_statements")
    usage_replay_counter(False)
    op.execute(
        "DROP INDEX billing.attempts_cost_audit,billing.adjustments_source_posted,"
        "billing.ledger_reversals"
    )
    op.execute("DROP TRIGGER resolved_attempt_review ON billing.request_attempts")
    op.execute("DROP FUNCTION billing.close_cost_review()")
    op.execute("DROP TRIGGER corrected_cost_report ON billing.cost_corrections")
    op.execute("DROP FUNCTION billing.dirty_corrected_cost()")
    op.execute("DROP VIEW billing.report_source")
    op.execute("ALTER TABLE billing.request_runs DROP COLUMN review_decision_id")
    op.execute("ALTER TABLE catalog.audit_events DROP COLUMN organization_id")
    op.execute("DROP TABLE billing.account_restrictions,billing.ingest_counters")
    for table, trigger in [
        ("request_runs", "request_report"),
        ("request_attempts", "attempt_report"),
        ("usage_events", "fact_report"),
        ("request_runs", "request_review"),
    ]:
        op.execute(f"DROP TRIGGER {trigger} ON billing.{table}")
    op.execute("DROP TRIGGER account_check ON billing.accounts")
    op.execute(
        "DROP FUNCTION billing.mark_report_change(), billing.track_request_review(),"
        "billing.track_account_check()"
    )
    op.execute(
        "ALTER TABLE billing.payment_cases DROP COLUMN version,DROP COLUMN "
        "decision_id,DROP COLUMN decision_basis"
    )
    op.execute(
        "DROP TABLE billing.cost_corrections,billing.review_cases,"
        "billing.case_decisions,billing.account_checks,billing.report_changes"
    )
    op.execute(
        "DROP TABLE reporting.export_jobs,reporting.request_facts,"
        "reporting.audit_events,publication.audit_events"
    )
