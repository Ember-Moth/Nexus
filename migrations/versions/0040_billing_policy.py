"""The billing policy row: currency, vector storage prepayment and payment limits.

``billing.settings`` becomes a complete, versioned policy row created here with USD. The
currency can change only until the first account, price, receiving account or payment channel
exists (checked by billing). ``managed_sha256`` and ``managed_at`` name the policy file that
locked the row through ``nexus settings import``.
"""

from alembic import op

revision = "0040_billing_policy"
down_revision = "0039_identity_policy"
branch_labels = None
depends_on = None

# (column, default, minimum, maximum)
LIMITS = (
    ("vector_storage_prepaid_days", 7, 1, 31),
    ("payment_requests_per_merchant_per_minute", 120, 1, 10000),
    ("payment_notification_per_channel", 1000, 1, 100000),
    ("payment_notification_per_ip", 120, 1, 10000),
)
ADDED = [name for name, *_ in LIMITS] + [
    "vector_storage_lapse_policy",
    "version",
    "managed_sha256",
    "managed_at",
]


def upgrade():
    columns = [
        f"ADD COLUMN {name} integer NOT NULL DEFAULT {default} "
        f"CONSTRAINT settings_{name}_check CHECK({name} BETWEEN {low} AND {high})"
        for name, default, low, high in LIMITS
    ]
    columns += [
        "ADD COLUMN vector_storage_lapse_policy text NOT NULL DEFAULT 'delete' "
        "CONSTRAINT settings_vector_storage_lapse_policy_check "
        "CHECK(vector_storage_lapse_policy IN ('delete','suspend'))",
        "ADD COLUMN version integer NOT NULL DEFAULT 1 CONSTRAINT settings_version_check "
        "CHECK(version > 0)",
        "ADD COLUMN managed_sha256 text CONSTRAINT settings_managed_sha256_check "
        "CHECK(managed_sha256 ~ '^[0-9a-f]{64}$')",
        "ADD COLUMN managed_at timestamptz",
        "ADD CONSTRAINT settings_managed_check "
        "CHECK((managed_sha256 IS NULL) = (managed_at IS NULL))",
        "ADD CONSTRAINT settings_currency_check CHECK(currency ~ '^[A-Z]{3}$')",
        "ALTER COLUMN currency SET DEFAULT 'USD'",
    ]
    op.execute("ALTER TABLE billing.settings " + ", ".join(columns))
    op.execute("INSERT INTO billing.settings(id) VALUES (1) ON CONFLICT DO NOTHING")
    op.execute(f"GRANT UPDATE(currency,{','.join(ADDED)}) ON billing.settings TO nexus_billing")


def downgrade():
    # Dropping the columns also drops their column privileges.
    op.execute("REVOKE UPDATE(currency) ON billing.settings FROM nexus_billing")
    op.execute(
        "ALTER TABLE billing.settings DROP CONSTRAINT settings_currency_check, "
        "ALTER COLUMN currency DROP DEFAULT, " + ", ".join(f"DROP COLUMN {name}" for name in ADDED)
    )
