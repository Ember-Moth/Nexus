"""Payment configuration, durable remote intents, verified evidence and shared funding."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0007_payment_admission"
down_revision = "0006_pricing_reservations"
branch_labels = None
depends_on = None
MONEY = sa.Numeric(28, 12)


def col(name, kind=None, nullable=False, **kw):
    return sa.Column(name, kind if kind is not None else sa.Uuid(), nullable=nullable, **kw)


def record():
    return [
        col("id", primary_key=True),
        col("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    ]


def moment(name, nullable=True):
    return col(name, sa.DateTime(timezone=True), nullable)


def money(name, default=None):
    return col(name, MONEY, server_default=default)


def upgrade():
    op.create_table(
        "payment_channels",
        *record(),
        col("version", sa.Integer(), server_default="1"),
        col("name", sa.String(200)),
        col("adapter_type", sa.String(32)),
        col("stable_merchant_id", sa.String(100)),
        col("environment", sa.String(10)),
        col("receipt_namespace", sa.String(160)),
        col("currency", sa.String(3)),
        money("min_amount"),
        money("max_amount"),
        col("status", sa.String(20), server_default="active"),
        col("active_config_id"),
        sa.CheckConstraint(
            "environment IN ('test','live') AND status IN "
            "('active','disabled') AND min_amount > 0 AND min_amount <= "
            "max_amount AND max_amount <> 'NaN'::numeric"
        ),
        schema="billing",
    )
    op.create_table(
        "payment_channel_versions",
        *record(),
        col("channel_id"),
        col("version", sa.Integer()),
        col("key_version", sa.Integer()),
        col("ciphertext", sa.LargeBinary()),
        col("public_configuration", JSONB()),
        col("capabilities", JSONB()),
        moment("verification_valid_until"),
        moment("revoked_at"),
        sa.ForeignKeyConstraint(["channel_id"], ["billing.payment_channels.id"]),
        sa.UniqueConstraint("channel_id", "version"),
        sa.UniqueConstraint("channel_id", "id"),
        schema="billing",
    )
    op.create_foreign_key(
        "payment_channel_active_version",
        "payment_channels",
        "payment_channel_versions",
        ["id", "active_config_id"],
        ["channel_id", "id"],
        source_schema="billing",
        referent_schema="billing",
        deferrable=True,
        initially="DEFERRED",
    )
    op.create_table(
        "payment_orders",
        *record(),
        col("version", sa.Integer(), server_default="1"),
        col("organization_id"),
        col("account_id"),
        col("channel_id"),
        col("config_version_id"),
        col("merchant_order_ref", sa.String(80), unique=True),
        col("stable_merchant_id", sa.String(100)),
        col("environment", sa.String(10)),
        col("receipt_namespace", sa.String(160)),
        money("amount"),
        money("credit_amount"),
        col("currency", sa.String(3)),
        col("created_by"),
        col("callback_url", sa.String(2048)),
        col("return_url", sa.String(2048)),
        moment("expires_at", False),
        col("provider_payment_ref", sa.String(255), True),
        col("checkout_result", JSONB(), True),
        col("checkout_generation", sa.BigInteger(), server_default="0"),
        col("checkout_state", sa.String(20), server_default="creating"),
        col("payment_state", sa.String(30), server_default="pending"),
        col("credit_state", sa.String(20), server_default="uncredited"),
        col("review_required", sa.Boolean(), server_default=sa.false()),
        col("review_reason", sa.String(100), True),
        col("block_credit", sa.Boolean(), server_default=sa.false()),
        money("reversed_amount", "0"),
        money("disputed_amount", "0"),
        col("funding_record_id", nullable=True),
        col("ledger_entry_id", nullable=True),
        moment("credited_at"),
        moment("last_observed_at"),
        sa.ForeignKeyConstraint(
            ["organization_id", "account_id", "currency"],
            [
                "billing.accounts.organization_id",
                "billing.accounts.id",
                "billing.accounts.currency",
            ],
        ),
        sa.ForeignKeyConstraint(
            ["channel_id", "config_version_id"],
            ["billing.payment_channel_versions.channel_id", "billing.payment_channel_versions.id"],
        ),
        sa.UniqueConstraint("organization_id", "account_id", "id"),
        sa.UniqueConstraint("organization_id", "account_id", "id", "currency"),
        sa.CheckConstraint(
            "amount > 0 AND amount <> 'NaN'::numeric AND "
            "credit_amount=amount AND reversed_amount >= 0 AND "
            "disputed_amount >= 0 AND reversed_amount <> 'NaN'::numeric AND "
            "disputed_amount <> 'NaN'::numeric"
        ),
        sa.CheckConstraint(
            "checkout_state IN ('creating','ready','closed','unknown') AND "
            "payment_state IN "
            "('pending','processing','succeeded','failed','cancelled','partially_reversed','reversed','unknown')"
            " AND credit_state IN ('uncredited','credited') AND "
            "(credit_state <> 'credited' OR (funding_record_id IS NOT NULL "
            "AND ledger_entry_id IS NOT NULL AND credited_at IS NOT NULL))"
        ),
        schema="billing",
    )
    op.create_index(
        "payment_orders_page",
        "payment_orders",
        ["organization_id", "created_at", "id"],
        schema="billing",
    )
    op.create_index(
        "payment_orders_namespace",
        "payment_orders",
        ["receipt_namespace", "merchant_order_ref"],
        schema="billing",
    )
    op.create_table(
        "payment_operations",
        *record(),
        col("order_id"),
        col("kind", sa.String(20)),
        col("operation_key", sa.String(80), server_default="main"),
        col("target_provider_ref", sa.String(255), True),
        col("state", sa.String(20), server_default="pending"),
        col("idempotency_key", sa.String(80)),
        col("fencing_token", sa.BigInteger(), server_default="0"),
        moment("lease_until"),
        moment("remote_started_at"),
        col("retry_observation_id", nullable=True),
        col("idempotency_mode", sa.String(30), True),
        moment("idempotency_expires_at"),
        col("attempts", sa.Integer(), server_default="0"),
        moment("next_attempt_at", False),
        col("wake_version", sa.BigInteger(), server_default="0"),
        col("claimed_wake_version", sa.BigInteger(), server_default="0"),
        col("requested_by", nullable=True),
        col("last_error", sa.String(80), True),
        sa.ForeignKeyConstraint(["order_id"], ["billing.payment_orders.id"]),
        sa.UniqueConstraint("order_id", "kind", "operation_key"),
        sa.UniqueConstraint("order_id", "id", name="payment_operation_scope"),
        sa.CheckConstraint(
            "kind IN ('create','query') AND state IN "
            "('pending','running','done','review') AND fencing_token >= 0 "
            "AND attempts >= 0"
        ),
        schema="billing",
    )
    op.create_index(
        "payment_operations_due",
        "payment_operations",
        ["next_attempt_at", "id"],
        schema="billing",
        postgresql_where=sa.text("state IN ('pending','running')"),
    )
    op.create_table(
        "payment_rate_buckets",
        col("receipt_namespace", sa.String(160), primary_key=True),
        moment("window_start", False),
        col("request_count", sa.Integer()),
        sa.CheckConstraint("request_count >= 0"),
        schema="billing",
    )
    op.execute("GRANT SELECT, INSERT, UPDATE ON billing.payment_rate_buckets TO nexus_billing")
    op.create_table(
        "payment_events",
        *record(),
        col("channel_id"),
        col("verification_version_id"),
        col("receipt_namespace", sa.String(160)),
        col("order_id", nullable=True),
        col("operation_id", nullable=True),
        col("wake_version", sa.BigInteger(), server_default="0"),
        col("event_key", sa.String(320)),
        col("event_fingerprint", sa.String(64)),
        col("body_digest", sa.String(64)),
        col("verified_fields", JSONB()),
        col("processing_state", sa.String(20)),
        sa.ForeignKeyConstraint(
            ["channel_id", "verification_version_id"],
            ["billing.payment_channel_versions.channel_id", "billing.payment_channel_versions.id"],
        ),
        sa.ForeignKeyConstraint(["order_id"], ["billing.payment_orders.id"]),
        sa.ForeignKeyConstraint(
            ["order_id", "operation_id"],
            ["billing.payment_operations.order_id", "billing.payment_operations.id"],
        ),
        sa.UniqueConstraint("receipt_namespace", "event_key", "event_fingerprint"),
        sa.CheckConstraint(
            "processing_state IN ('received','ignored','conflict','processed','review')"
        ),
        schema="billing",
    )
    op.create_table(
        "payment_observations",
        *record(),
        col("order_id"),
        col("config_version_id"),
        col("source", sa.String(30)),
        col("order_version", sa.Integer()),
        col("checkout_generation", sa.BigInteger()),
        moment("observed_at", False),
        col("evidence_digest", sa.String(64)),
        col("facts", JSONB()),
        col("disposition", sa.String(30)),
        sa.ForeignKeyConstraint(["order_id"], ["billing.payment_orders.id"]),
        sa.ForeignKeyConstraint(["config_version_id"], ["billing.payment_channel_versions.id"]),
        schema="billing",
    )
    op.create_unique_constraint(
        "payment_observation_scope", "payment_observations", ["order_id", "id"], schema="billing"
    )
    op.create_foreign_key(
        "payment_retry_observation",
        "payment_operations",
        "payment_observations",
        ["order_id", "retry_observation_id"],
        ["order_id", "id"],
        source_schema="billing",
        referent_schema="billing",
    )
    op.create_table(
        "payment_receipts",
        *record(),
        col("organization_id"),
        col("account_id"),
        col("order_id"),
        col("receipt_namespace", sa.String(160)),
        col("transaction_id", sa.String(255)),
        col("provider_payment_ref", sa.String(255)),
        money("amount"),
        col("currency", sa.String(3)),
        col("observation_id"),
        moment("confirmed_at", False),
        sa.ForeignKeyConstraint(
            ["organization_id", "account_id", "order_id", "currency"],
            [
                "billing.payment_orders.organization_id",
                "billing.payment_orders.account_id",
                "billing.payment_orders.id",
                "billing.payment_orders.currency",
            ],
        ),
        sa.ForeignKeyConstraint(["observation_id"], ["billing.payment_observations.id"]),
        sa.UniqueConstraint("receipt_namespace", "transaction_id"),
        sa.UniqueConstraint("organization_id", "account_id", "order_id", "id"),
        sa.CheckConstraint("amount > 0 AND amount <> 'NaN'::numeric"),
        schema="billing",
    )
    op.create_table(
        "payment_cases",
        *record(),
        col("organization_id"),
        col("order_id"),
        col("reason", sa.String(100)),
        col("status", sa.String(20), server_default="open"),
        money("amount_at_risk", "0"),
        col("evidence", JSONB()),
        sa.ForeignKeyConstraint(["order_id"], ["billing.payment_orders.id"]),
        sa.UniqueConstraint("order_id", "reason"),
        sa.CheckConstraint(
            "status IN ('open','resolved') AND amount_at_risk >= 0 AND "
            "amount_at_risk <> 'NaN'::numeric"
        ),
        schema="billing",
    )
    op.alter_column("funding_records", "receiving_account_id", nullable=True, schema="billing")
    op.alter_column("funding_records", "created_by", nullable=True, schema="billing")
    op.alter_column("funding_records", "receipt_namespace", type_=sa.String(160), schema="billing")
    op.add_column(
        "funding_records",
        col("source_type", sa.String(20), server_default="manual"),
        schema="billing",
    )
    op.add_column("funding_records", col("system_actor", sa.String(80), True), schema="billing")
    op.add_column("funding_records", col("payment_order_id", nullable=True), schema="billing")
    op.add_column("funding_records", col("payment_receipt_id", nullable=True), schema="billing")
    op.create_unique_constraint(
        "funding_order_unique", "funding_records", ["payment_order_id"], schema="billing"
    )
    op.create_unique_constraint(
        "funding_payment_receipt_unique",
        "funding_records",
        ["payment_receipt_id"],
        schema="billing",
    )
    op.create_unique_constraint(
        "funding_account_scope",
        "funding_records",
        ["organization_id", "account_id", "id"],
        schema="billing",
    )
    op.create_foreign_key(
        "funding_receipt_scope",
        "funding_records",
        "payment_receipts",
        ["organization_id", "account_id", "payment_order_id", "payment_receipt_id"],
        ["organization_id", "account_id", "order_id", "id"],
        source_schema="billing",
        referent_schema="billing",
    )
    op.create_check_constraint(
        "funding_source_actor",
        "funding_records",
        "(source_type='manual' AND receiving_account_id IS NOT NULL AND "
        "created_by IS NOT NULL AND system_actor IS NULL AND "
        "payment_order_id IS NULL AND payment_receipt_id IS NULL) OR "
        "(source_type='gateway' AND receiving_account_id IS NULL AND "
        "created_by IS NULL AND posted_by IS NULL AND system_actor IS NOT "
        "NULL AND system_actor='payment-worker' AND payment_order_id IS NOT "
        "NULL AND payment_receipt_id IS NOT NULL)",
        schema="billing",
    )
    op.create_foreign_key(
        "order_funding_scope",
        "payment_orders",
        "funding_records",
        ["organization_id", "account_id", "funding_record_id"],
        ["organization_id", "account_id", "id"],
        source_schema="billing",
        referent_schema="billing",
    )
    op.create_foreign_key(
        "order_ledger_scope",
        "payment_orders",
        "ledger_entries",
        ["organization_id", "account_id", "ledger_entry_id"],
        ["organization_id", "account_id", "id"],
        source_schema="billing",
        referent_schema="billing",
    )
    op.execute("""
        CREATE FUNCTION billing.protect_payment_channel() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN
            IF ROW(OLD.id,OLD.adapter_type,OLD.stable_merchant_id,OLD.environment,
                   OLD.receipt_namespace,OLD.currency)
                IS DISTINCT FROM ROW(NEW.id,NEW.adapter_type,NEW.stable_merchant_id,NEW.environment,
                   NEW.receipt_namespace,NEW.currency) THEN
                RAISE EXCEPTION 'payment channel binding is immutable' USING ERRCODE='23514';
            END IF; RETURN NEW;
        END $$
    """)
    op.execute(
        "CREATE TRIGGER payment_channel_binding BEFORE UPDATE ON "
        "billing.payment_channels FOR EACH ROW EXECUTE FUNCTION "
        "billing.protect_payment_channel()"
    )
    op.execute("""
        CREATE FUNCTION billing.protect_payment_config() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN
            IF ROW(OLD.id,OLD.channel_id,OLD.version,OLD.key_version,OLD.ciphertext,
                   OLD.public_configuration,OLD.capabilities,OLD.created_at)
                IS DISTINCT FROM ROW(NEW.id,NEW.channel_id,NEW.version,NEW.key_version,
                   NEW.ciphertext,
                   NEW.public_configuration,NEW.capabilities,NEW.created_at) THEN
                RAISE EXCEPTION 'payment config is immutable' USING ERRCODE='23514';
            END IF;
            IF OLD.revoked_at IS NOT NULL AND NEW.revoked_at IS DISTINCT FROM OLD.revoked_at THEN
                RAISE EXCEPTION 'revoked payment credentials cannot be restored'
                    USING ERRCODE='23514';
            END IF; RETURN NEW;
        END $$
    """)
    op.execute(
        "CREATE TRIGGER payment_config_immutable BEFORE UPDATE ON "
        "billing.payment_channel_versions FOR EACH ROW EXECUTE FUNCTION "
        "billing.protect_payment_config()"
    )
    op.execute("""
        CREATE FUNCTION billing.protect_payment_order() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN
            IF ROW(OLD.id,OLD.organization_id,OLD.account_id,OLD.channel_id,OLD.config_version_id,
                   OLD.merchant_order_ref,OLD.stable_merchant_id,OLD.environment,
                   OLD.receipt_namespace,
                   OLD.amount,OLD.credit_amount,OLD.currency,OLD.created_by,OLD.callback_url,
                   OLD.return_url,OLD.expires_at,OLD.created_at)
                IS DISTINCT FROM ROW(NEW.id,NEW.organization_id,NEW.account_id,NEW.channel_id,
                   NEW.config_version_id,NEW.merchant_order_ref,NEW.stable_merchant_id,NEW.environment,
                   NEW.receipt_namespace,NEW.amount,NEW.credit_amount,NEW.currency,NEW.created_by,
                   NEW.callback_url,NEW.return_url,NEW.expires_at,NEW.created_at) THEN
                RAISE EXCEPTION 'payment expectation is immutable' USING ERRCODE='23514';
            END IF;
            IF OLD.credit_state='credited' AND ROW(OLD.credit_state,OLD.funding_record_id,
                OLD.ledger_entry_id,OLD.credited_at) IS DISTINCT FROM ROW(NEW.credit_state,
                NEW.funding_record_id,NEW.ledger_entry_id,NEW.credited_at) THEN
                RAISE EXCEPTION 'original payment credit is immutable' USING ERRCODE='23514';
            END IF; RETURN NEW;
        END $$
    """)
    op.execute(
        "CREATE TRIGGER payment_order_immutable BEFORE UPDATE ON "
        "billing.payment_orders FOR EACH ROW EXECUTE FUNCTION "
        "billing.protect_payment_order()"
    )
    op.execute("""
        CREATE FUNCTION billing.protect_payment_operation() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN
            IF ROW(OLD.id,OLD.order_id,OLD.kind,OLD.operation_key,OLD.idempotency_key,
                OLD.target_provider_ref) IS DISTINCT FROM ROW(NEW.id,NEW.order_id,NEW.kind,
                NEW.operation_key,NEW.idempotency_key,NEW.target_provider_ref)
                OR NEW.fencing_token < OLD.fencing_token THEN
                RAISE EXCEPTION 'payment operation binding is immutable' USING ERRCODE='23514';
            END IF;
            IF OLD.remote_started_at IS NOT NULL AND ROW(OLD.remote_started_at,
                OLD.idempotency_mode,OLD.idempotency_expires_at) IS DISTINCT FROM
                ROW(NEW.remote_started_at,NEW.idempotency_mode,NEW.idempotency_expires_at) THEN
                RAISE EXCEPTION 'original creation intent is immutable' USING ERRCODE='23514';
            END IF; RETURN NEW;
        END $$
    """)
    op.execute(
        "CREATE TRIGGER payment_operation_intent BEFORE UPDATE ON billing.payment_operations "
        "FOR EACH ROW EXECUTE FUNCTION billing.protect_payment_operation()"
    )
    op.execute("""
        CREATE FUNCTION billing.protect_payment_event() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN
            IF ROW(OLD.id,OLD.channel_id,OLD.verification_version_id,OLD.receipt_namespace,
                OLD.order_id,OLD.operation_id,OLD.wake_version,OLD.event_key,OLD.event_fingerprint,
                OLD.body_digest,OLD.verified_fields,OLD.created_at)
            IS DISTINCT FROM ROW(NEW.id,NEW.channel_id,NEW.verification_version_id,
                NEW.receipt_namespace,NEW.order_id,NEW.operation_id,NEW.wake_version,
                NEW.event_key,NEW.event_fingerprint,NEW.body_digest,NEW.verified_fields,NEW.created_at)
            THEN RAISE EXCEPTION 'payment notice is immutable' USING ERRCODE='23514'; END IF;
            IF OLD.processing_state IN ('processed','ignored')
                AND NEW.processing_state <> OLD.processing_state THEN
                RAISE EXCEPTION 'completed notice cannot regress' USING ERRCODE='23514';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute(
        "CREATE TRIGGER payment_event_immutable BEFORE UPDATE ON billing.payment_events "
        "FOR EACH ROW EXECUTE FUNCTION billing.protect_payment_event()"
    )
    op.execute("GRANT SELECT, INSERT, UPDATE ON billing.payment_events TO nexus_billing")
    op.execute(
        "CREATE TRIGGER payment_event_no_delete BEFORE DELETE ON billing.payment_events "
        "FOR EACH ROW EXECUTE FUNCTION billing.reject_immutable_change()"
    )
    for name in ("payment_observations", "payment_receipts"):
        op.execute(
            f"CREATE TRIGGER {name}_immutable BEFORE UPDATE OR DELETE ON billing.{name} "
            "FOR EACH ROW EXECUTE FUNCTION billing.reject_immutable_change()"
        )
        op.execute(f"GRANT SELECT, INSERT ON billing.{name} TO nexus_billing")
    for name in (
        "payment_channels",
        "payment_channel_versions",
        "payment_orders",
        "payment_operations",
        "payment_cases",
    ):
        op.execute(f"GRANT SELECT, INSERT, UPDATE ON billing.{name} TO nexus_billing")


def downgrade():
    # Erasing remote intents can lose a real late payment. Never silently discard them.
    op.execute("""
        DO $$ BEGIN IF EXISTS(SELECT 1 FROM billing.payment_orders) THEN
            RAISE EXCEPTION 'cannot downgrade while payment orders are retained';
        END IF; END $$
    """)
    op.drop_constraint("payment_retry_observation", "payment_operations", schema="billing")
    op.drop_constraint("order_ledger_scope", "payment_orders", schema="billing")
    op.drop_constraint("order_funding_scope", "payment_orders", schema="billing")
    for name in (
        "funding_source_actor",
        "funding_receipt_scope",
        "funding_account_scope",
        "funding_payment_receipt_unique",
        "funding_order_unique",
    ):
        op.drop_constraint(name, "funding_records", schema="billing")
    for name in ("payment_receipt_id", "payment_order_id", "system_actor", "source_type"):
        op.drop_column("funding_records", name, schema="billing")
    op.alter_column("funding_records", "receiving_account_id", nullable=False, schema="billing")
    op.alter_column("funding_records", "created_by", nullable=False, schema="billing")
    op.alter_column("funding_records", "receipt_namespace", type_=sa.String(80), schema="billing")
    op.drop_constraint("payment_channel_active_version", "payment_channels", schema="billing")
    for name in (
        "payment_cases",
        "payment_receipts",
        "payment_observations",
        "payment_events",
        "payment_rate_buckets",
        "payment_operations",
        "payment_orders",
        "payment_channel_versions",
        "payment_channels",
    ):
        op.drop_table(name, schema="billing")
    for name in (
        "protect_payment_order",
        "protect_payment_config",
        "protect_payment_channel",
        "protect_payment_event",
        "protect_payment_operation",
    ):
        op.execute(f"DROP FUNCTION billing.{name}()")
