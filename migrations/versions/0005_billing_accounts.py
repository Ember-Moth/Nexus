"""Prepaid accounts, explicit finance permissions and reliable account provisioning."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0005_billing_accounts"
down_revision = "0004_catalog_and_access"
branch_labels = None
depends_on = None
MONEY = sa.Numeric(28, 12)


def created():
    return sa.Column(
        "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    )


def upgrade():
    op.add_column(
        "users",
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        schema="identity",
    )
    op.add_column("organizations", sa.Column("billing_account_id", sa.Uuid()), schema="identity")
    op.create_table(
        "inbox",
        sa.Column("consumer", sa.String(80), primary_key=True),
        sa.Column("event_id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("result_id", sa.Uuid(), nullable=False),
        created(),
        schema="identity",
    )
    op.execute("GRANT SELECT, INSERT ON identity.inbox TO nexus_identity")
    op.create_table(
        "settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("currency", sa.String(3), nullable=False),
        created(),
        sa.CheckConstraint("id=1", name="billing_settings_singleton"),
        schema="billing",
    )
    op.create_table(
        "accounts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False, unique=True),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("balance", MONEY, nullable=False, server_default="0"),
        sa.Column("held_amount", MONEY, nullable=False, server_default="0"),
        sa.Column("entry_sequence", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        created(),
        sa.UniqueConstraint("organization_id", "id", name="billing_account_scope"),
        sa.UniqueConstraint("organization_id", "id", "currency", name="billing_account_currency"),
        sa.CheckConstraint(
            (
                "balance >= held_amount AND held_amount >= 0 AND balance <> "
                "'NaN'::numeric AND held_amount <> 'NaN'::numeric"
            ),
            name="account_funds",
        ),
        sa.CheckConstraint(
            "status IN ('active','frozen') AND version > 0 AND entry_sequence >= 0",
            name="account_state",
        ),
        schema="billing",
    )
    op.create_table(
        "inbox",
        sa.Column("consumer", sa.String(80), primary_key=True),
        sa.Column("event_id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("result_id", sa.Uuid(), nullable=False),
        created(),
        schema="billing",
    )
    op.create_table(
        "outbox",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("topic", sa.String(80), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.CheckConstraint("schema_version > 0", name="billing_event_version"),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("aggregate_id", sa.Uuid(), nullable=False),
        sa.Column("payload", JSONB(), nullable=False),
        created(),
        sa.Column("delivered_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("topic", "aggregate_id", name="billing_outbox_fact"),
        schema="billing",
    )
    op.create_table(
        "receiving_accounts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("reference", sa.String(100), nullable=False, unique=True),
        sa.Column("receipt_namespace", sa.String(80), nullable=False, unique=True),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        created(),
        sa.CheckConstraint("status IN ('active','disabled')", name="receiving_account_state"),
        schema="billing",
    )
    op.create_table(
        "funding_records",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column(
            "receiving_account_id",
            sa.Uuid(),
            sa.ForeignKey("billing.receiving_accounts.id"),
            nullable=False,
        ),
        sa.Column("receipt_namespace", sa.String(80), nullable=False),
        sa.Column("external_reference", sa.String(255), nullable=False),
        sa.Column("amount", MONEY, nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("evidence", sa.String(2000), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="draft"),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("posted_by", sa.Uuid()),
        sa.Column("posted_at", sa.DateTime(timezone=True)),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        created(),
        sa.ForeignKeyConstraint(
            ["organization_id", "account_id", "currency"],
            [
                "billing.accounts.organization_id",
                "billing.accounts.id",
                "billing.accounts.currency",
            ],
        ),
        sa.CheckConstraint("amount > 0 AND amount <> 'NaN'::numeric", name="funding_amount"),
        sa.CheckConstraint("status IN ('draft','posted','cancelled')", name="funding_state"),
        schema="billing",
    )
    op.create_index(
        "funding_receipt_unique",
        "funding_records",
        ["receipt_namespace", "external_reference"],
        unique=True,
        schema="billing",
        postgresql_where=sa.text("status <> 'cancelled'"),
    )
    op.create_table(
        "ledger_entries",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("signed_amount", MONEY, nullable=False),
        sa.Column("balance_after", MONEY, nullable=False),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("source_type", sa.String(30), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("reverses_entry_id", sa.Uuid(), sa.ForeignKey("billing.ledger_entries.id")),
        sa.Column("actor_id", sa.Uuid()),
        sa.Column(
            "posted_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.clock_timestamp(),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "account_id", "currency"],
            [
                "billing.accounts.organization_id",
                "billing.accounts.id",
                "billing.accounts.currency",
            ],
        ),
        sa.UniqueConstraint("source_type", "source_id", name="ledger_source_unique"),
        sa.UniqueConstraint("account_id", "sequence", name="ledger_sequence_unique"),
        sa.UniqueConstraint("organization_id", "account_id", "id", name="ledger_scope"),
        sa.ForeignKeyConstraint(
            ["organization_id", "account_id", "reverses_entry_id"],
            [
                "billing.ledger_entries.organization_id",
                "billing.ledger_entries.account_id",
                "billing.ledger_entries.id",
            ],
        ),
        sa.CheckConstraint(
            "(kind='funding_credit' AND source_type='funding' AND signed_amount>0 "
            "AND reverses_entry_id IS NULL) OR "
            "(kind='usage_debit' AND source_type='request' AND signed_amount<0 "
            "AND reverses_entry_id IS NULL) OR "
            "(kind='usage_refund' AND source_type='adjustment' AND signed_amount>0 "
            "AND reverses_entry_id IS NOT NULL) OR "
            "(kind='funding_reversal' AND source_type='adjustment' AND signed_amount<0 "
            "AND reverses_entry_id IS NOT NULL)",
            name="ledger_source_direction",
        ),
        sa.CheckConstraint(
            (
                "signed_amount <> 0 AND signed_amount <> 'NaN'::numeric AND "
                "balance_after >= 0 AND balance_after <> 'NaN'::numeric"
            ),
            name="ledger_amount",
        ),
        schema="billing",
    )
    op.create_table(
        "adjustments",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("account_id", sa.Uuid(), nullable=False),
        sa.Column(
            "source_entry_id", sa.Uuid(), sa.ForeignKey("billing.ledger_entries.id"), nullable=False
        ),
        sa.Column("amount", MONEY, nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("reason", sa.String(2000), nullable=False),
        sa.Column("evidence", sa.String(2000), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="draft"),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("posted_by", sa.Uuid()),
        sa.Column("posted_at", sa.DateTime(timezone=True)),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        created(),
        sa.ForeignKeyConstraint(
            ["organization_id", "account_id", "currency"],
            [
                "billing.accounts.organization_id",
                "billing.accounts.id",
                "billing.accounts.currency",
            ],
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "account_id", "source_entry_id"],
            [
                "billing.ledger_entries.organization_id",
                "billing.ledger_entries.account_id",
                "billing.ledger_entries.id",
            ],
        ),
        sa.CheckConstraint(
            "amount > 0 AND amount <> 'NaN'::numeric AND status IN ('draft','posted')",
            name="adjustment_state",
        ),
        schema="billing",
    )
    op.create_table(
        "idempotency_records",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("actor_id", sa.Uuid(), nullable=False),
        sa.Column("scope", sa.String(160), nullable=False),
        sa.Column("key_digest", sa.String(64), nullable=False),
        sa.Column("request_digest", sa.String(64), nullable=False),
        sa.Column("resource_kind", sa.String(40), nullable=False),
        sa.Column("resource_id", sa.Uuid(), nullable=False),
        created(),
        sa.UniqueConstraint("actor_id", "scope", "key_digest", name="billing_idempotency_unique"),
        schema="billing",
    )
    op.create_table(
        "audit_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("organization_id", sa.Uuid()),
        sa.Column("actor_id", sa.Uuid()),
        sa.Column("action", sa.String(100), nullable=False),
        sa.Column("resource_id", sa.Uuid(), nullable=False),
        sa.Column("details", JSONB(), nullable=False),
        created(),
        schema="billing",
    )
    op.execute("""
        CREATE FUNCTION billing.protect_receiving_identity() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN
            IF ROW(OLD.id, OLD.reference, OLD.receipt_namespace, OLD.currency)
                IS DISTINCT FROM ROW(NEW.id, NEW.reference, NEW.receipt_namespace, NEW.currency)
            THEN RAISE EXCEPTION 'receipt namespace is immutable' USING ERRCODE='23514'; END IF;
            RETURN NEW;
        END $$
    """)
    op.execute(
        "CREATE TRIGGER receiving_identity BEFORE UPDATE ON billing.receiving_accounts "
        "FOR EACH ROW EXECUTE FUNCTION billing.protect_receiving_identity()"
    )
    op.execute("""
        CREATE FUNCTION billing.protect_account_identity() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN
            IF ROW(OLD.id, OLD.organization_id, OLD.currency)
                IS DISTINCT FROM ROW(NEW.id, NEW.organization_id, NEW.currency)
            THEN RAISE EXCEPTION 'account identity is immutable' USING ERRCODE='23514'; END IF;
            RETURN NEW;
        END $$
    """)
    op.execute(
        "CREATE TRIGGER account_identity BEFORE UPDATE ON billing.accounts "
        "FOR EACH ROW EXECUTE FUNCTION billing.protect_account_identity()"
    )
    op.execute("""
        CREATE FUNCTION billing.reject_immutable_change() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN
            RAISE EXCEPTION 'immutable billing fact' USING ERRCODE='23514';
        END $$
    """)
    op.execute(
        "CREATE TRIGGER ledger_immutable BEFORE UPDATE OR DELETE ON billing.ledger_entries "
        "FOR EACH ROW EXECUTE FUNCTION billing.reject_immutable_change()"
    )
    op.execute("""
        CREATE FUNCTION billing.protect_posted_record() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN
            IF OLD.status = 'posted' THEN
                RAISE EXCEPTION 'posted record is immutable' USING ERRCODE='23514';
            END IF;
            IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
            RETURN NEW;
        END $$
    """)
    for name in ("funding_records", "adjustments"):
        op.execute(
            f"CREATE TRIGGER {name}_posted BEFORE UPDATE OR DELETE ON billing.{name} "
            "FOR EACH ROW EXECUTE FUNCTION billing.protect_posted_record()"
        )
    for name in ("accounts", "receiving_accounts", "funding_records", "adjustments", "outbox"):
        op.execute(f"GRANT SELECT, INSERT, UPDATE ON billing.{name} TO nexus_billing")
    for name in ("settings", "inbox", "ledger_entries", "idempotency_records", "audit_events"):
        op.execute(f"GRANT SELECT, INSERT ON billing.{name} TO nexus_billing")
    for name in ("funding_records", "adjustments"):
        op.create_index(
            f"{name}_org_page", name, ["organization_id", "created_at", "id"], schema="billing"
        )


def downgrade():
    for name in (
        "audit_events",
        "idempotency_records",
        "adjustments",
        "ledger_entries",
        "funding_records",
        "receiving_accounts",
        "outbox",
        "inbox",
        "accounts",
        "settings",
    ):
        op.drop_table(name, schema="billing")
    op.execute("DROP FUNCTION billing.protect_account_identity()")
    op.execute("DROP FUNCTION billing.protect_receiving_identity()")
    op.execute("DROP FUNCTION billing.protect_posted_record()")
    op.execute("DROP FUNCTION billing.reject_immutable_change()")
    op.drop_table("inbox", schema="identity")
    op.drop_column("organizations", "billing_account_id", schema="identity")
    op.drop_column("users", "version", schema="identity")
