"""Freeze provider collection amounts independently of wallet currency."""

from alembic import op

revision = "0011_payment_quotes"
down_revision = "0010_settlement_backpressure"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        ALTER TABLE billing.payment_orders
          ADD COLUMN collection_amount numeric(28,12),
          ADD COLUMN collection_currency varchar(3),
          ADD COLUMN exchange_rate numeric(28,12) NOT NULL DEFAULT 1
    """)
    op.execute("""
        UPDATE billing.payment_orders SET collection_amount=amount, collection_currency=currency
    """)
    op.execute("""
        ALTER TABLE billing.payment_orders
          ALTER collection_amount SET NOT NULL, ALTER collection_currency SET NOT NULL,
          ADD CONSTRAINT valid_payment_quote CHECK (
            exchange_rate>0 AND exchange_rate<>'NaN'::numeric AND collection_amount>0 AND
            ((currency=collection_currency AND exchange_rate=1 AND collection_amount=amount)
             OR (currency='USD' AND collection_currency='CNY'
                 AND collection_amount=round(amount*exchange_rate,2)))),
          ADD UNIQUE (organization_id, account_id, id, collection_currency)
    """)
    # Workers still on the previous binary may insert same-currency orders while
    # applying this additive migration. Both omitted fields identify that write.
    op.execute("""
        CREATE FUNCTION billing.default_payment_quote() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN
          IF NEW.collection_amount IS NULL AND NEW.collection_currency IS NULL THEN
            NEW.collection_amount=NEW.amount;
            NEW.collection_currency=NEW.currency;
          END IF;
          RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER default_payment_quote BEFORE INSERT ON billing.payment_orders
        FOR EACH ROW EXECUTE FUNCTION billing.default_payment_quote()
    """)
    # PostgreSQL names the original anonymous scope FK. Look it up by its referenced table.
    op.execute("""
        DO $$ DECLARE fk name; BEGIN
          SELECT conname INTO STRICT fk FROM pg_constraint
           WHERE conrelid='billing.payment_receipts'::regclass AND contype='f'
             AND confrelid='billing.payment_orders'::regclass;
          EXECUTE format('ALTER TABLE billing.payment_receipts DROP CONSTRAINT %I',fk);
        END $$
    """)
    op.execute("""
        ALTER TABLE billing.payment_receipts ADD CONSTRAINT receipt_collection_scope
        FOREIGN KEY(organization_id,account_id,order_id,currency)
        REFERENCES billing.payment_orders(organization_id,account_id,id,collection_currency)
    """)
    op.execute("""
        CREATE TRIGGER payment_quote_immutable BEFORE UPDATE OF collection_amount,
          collection_currency,exchange_rate ON billing.payment_orders FOR EACH ROW
          WHEN ((OLD.collection_amount,OLD.collection_currency,OLD.exchange_rate)
             IS DISTINCT FROM (NEW.collection_amount,NEW.collection_currency,NEW.exchange_rate))
          EXECUTE FUNCTION billing.reject_immutable_change()
    """)


def downgrade():
    op.execute("""
        DO $$ BEGIN IF EXISTS (SELECT FROM billing.payment_orders
          WHERE collection_currency<>currency)
          THEN RAISE EXCEPTION 'cross-currency payment history is retained'; END IF; END $$
    """)
    op.execute("DROP TRIGGER default_payment_quote ON billing.payment_orders")
    op.execute("DROP FUNCTION billing.default_payment_quote()")
    op.execute("DROP TRIGGER payment_quote_immutable ON billing.payment_orders")
    op.execute("ALTER TABLE billing.payment_receipts DROP CONSTRAINT receipt_collection_scope")
    op.execute("""
        ALTER TABLE billing.payment_receipts ADD FOREIGN KEY
          (organization_id,account_id,order_id,currency)
        REFERENCES billing.payment_orders(organization_id,account_id,id,currency)
    """)
    op.execute("""
        ALTER TABLE billing.payment_orders DROP COLUMN collection_amount,
          DROP COLUMN collection_currency,DROP COLUMN exchange_rate
    """)
