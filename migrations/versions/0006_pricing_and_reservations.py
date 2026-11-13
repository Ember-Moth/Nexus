"""Immutable pricing and prepaid reservation/budget primitives."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0006_pricing_reservations"
down_revision = "0005_billing_accounts"
branch_labels = None
depends_on = None
MONEY = sa.Numeric(28, 12)
DEFAULT_DISCOUNT_ID = "00000000-0000-0000-0000-000000000001"


def col(name, kind=None, nullable=False, **kwargs):
    return sa.Column(name, kind if kind is not None else sa.Uuid(), nullable=nullable, **kwargs)


def record():
    return [
        col("id", primary_key=True),
        col("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    ]


def version():
    return col("version", sa.Integer(), server_default="1")


def date(name, nullable=False):
    return col(name, sa.DateTime(timezone=True), nullable)


def money(name, default=None, nullable=False):
    return col(name, MONEY, nullable, server_default=default)


def check_money(*names):
    return sa.CheckConstraint(
        " AND ".join(f"{name} >= 0 AND {name} <> 'NaN'::numeric" for name in names)
    )


def upgrade():
    op.create_table(
        "price_versions",
        *record(),
        version(),
        col("kind", sa.String(10)),
        col("scope_id"),
        col("model_id"),
        col("currency", sa.String(3)),
        col("rates", JSONB()),
        col("enabled", sa.Boolean(), server_default=sa.true()),
        date("effective_at", True),
        date("published_at", True),
        col("state", sa.String(20), server_default="draft"),
        col("created_by"),
        sa.CheckConstraint(
            "kind IN ('sale','cost') AND state IN ('draft','published') AND "
            "(state <> 'published' OR (effective_at IS NOT NULL AND "
            "published_at IS NOT NULL))"
        ),
        schema="billing",
    )
    op.create_index(
        "price_scope_start",
        "price_versions",
        ["kind", "scope_id", "effective_at"],
        unique=True,
        schema="billing",
        postgresql_where=sa.text("state='published'"),
    )
    op.create_table(
        "discount_versions",
        *record(),
        version(),
        col("organization_id", nullable=True),
        col("factor", sa.Numeric(7, 6)),
        date("effective_at", True),
        date("published_at", True),
        col("state", sa.String(20), server_default="draft"),
        col("created_by", nullable=True),
        sa.CheckConstraint(
            "factor > 0 AND factor <= 1 AND factor <> 'NaN'::numeric AND state "
            "IN ('draft','published') AND (state <> 'published' OR "
            "(effective_at IS NOT NULL AND published_at IS NOT NULL))"
        ),
        schema="billing",
    )
    op.create_index(
        "discount_scope_start",
        "discount_versions",
        ["organization_id", "effective_at"],
        unique=True,
        schema="billing",
        postgresql_where=sa.text("state='published'"),
    )
    op.create_index(
        "discount_default",
        "discount_versions",
        [sa.text("(organization_id IS NULL)")],
        unique=True,
        schema="billing",
        postgresql_where=sa.text("organization_id IS NULL"),
    )
    op.execute(
        "INSERT INTO billing.discount_versions (id, factor, state, effective_at, published_at) "
        f"VALUES ('{DEFAULT_DISCOUNT_ID}', 1, 'published', "
        "'1970-01-01T00:00:00Z', clock_timestamp())"
    )
    op.create_table(
        "budget_policies",
        *record(),
        version(),
        col("organization_id"),
        col("layer", sa.String(20)),
        col("scope_type", sa.String(20)),
        col("scope_id"),
        col("project_id", nullable=True),
        col("api_key_id", nullable=True),
        col("currency", sa.String(3)),
        money("limit_amount"),
        col("period", sa.String(10)),
        col("status", sa.String(20), server_default="active"),
        check_money("limit_amount"),
        sa.CheckConstraint(
            "layer IN ('platform','organization') AND scope_type IN "
            "('organization','project','key') AND period IN ('day','month') AND"
            " status IN ('active','disabled')"
        ),
        sa.UniqueConstraint("organization_id", "layer", "scope_type", "scope_id", "period"),
        sa.UniqueConstraint("organization_id", "id", name="budget_policy_scope"),
        sa.ForeignKeyConstraint(["organization_id"], ["billing.accounts.organization_id"]),
        sa.ForeignKeyConstraint(
            ["organization_id", "project_id"],
            ["identity.projects.organization_id", "identity.projects.id"],
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "api_key_id"],
            ["identity.api_keys.organization_id", "identity.api_keys.id"],
        ),
        sa.CheckConstraint(
            "(scope_type='organization' AND scope_id=organization_id "
            "AND project_id IS NULL AND api_key_id IS NULL) OR "
            "(scope_type='project' AND scope_id=project_id "
            "AND project_id IS NOT NULL AND api_key_id IS NULL) OR "
            "(scope_type='key' AND scope_id=api_key_id "
            "AND project_id IS NOT NULL AND api_key_id IS NOT NULL)",
            name="budget_scope_fields",
        ),
        schema="billing",
    )
    op.create_table(
        "budget_periods",
        *record(),
        col("organization_id"),
        col("policy_id"),
        col("policy_version", sa.Integer()),
        date("period_start"),
        date("period_end"),
        money("limit_amount"),
        money("spent", "0"),
        money("reserved", "0"),
        sa.ForeignKeyConstraint(
            ["organization_id", "policy_id"],
            ["billing.budget_policies.organization_id", "billing.budget_policies.id"],
        ),
        sa.UniqueConstraint("organization_id", "id", name="budget_period_scope"),
        sa.UniqueConstraint("policy_id", "period_start"),
        check_money("limit_amount", "spent", "reserved"),
        sa.CheckConstraint("period_end > period_start"),
        schema="billing",
    )
    op.create_table(
        "request_runs",
        *record(),
        col("organization_id"),
        col("account_id"),
        col("project_id"),
        col("service_account_id"),
        col("api_key_id"),
        col("model_id"),
        col("request_digest", sa.String(64)),
        col("sale_price_version_id"),
        col("discount_version_id"),
        col("billing_policy_version", sa.String(40)),
        col("input_limit", sa.BigInteger()),
        col("output_limit", sa.BigInteger()),
        money("reserved_amount"),
        money("charged_amount", nullable=True),
        money("refunded_amount", "0"),
        col("state", sa.String(30), server_default="reserved"),
        col("reservation_state", sa.String(20), server_default="held"),
        col("review_reason", sa.String(100), True),
        date("expires_at"),
        date("settled_at", True),
        col("settlement_digest", sa.String(64), True),
        col("charge_details", JSONB(), True),
        sa.ForeignKeyConstraint(
            ["organization_id", "account_id"],
            ["billing.accounts.organization_id", "billing.accounts.id"],
        ),
        sa.UniqueConstraint("organization_id", "id", name="request_scope"),
        sa.ForeignKeyConstraint(
            ["organization_id", "project_id", "service_account_id", "api_key_id"],
            [
                "identity.api_keys.organization_id",
                "identity.api_keys.project_id",
                "identity.api_keys.service_account_id",
                "identity.api_keys.id",
            ],
        ),
        sa.ForeignKeyConstraint(["sale_price_version_id"], ["billing.price_versions.id"]),
        sa.ForeignKeyConstraint(["discount_version_id"], ["billing.discount_versions.id"]),
        check_money("reserved_amount", "charged_amount", "refunded_amount"),
        sa.CheckConstraint(
            "state IN ('reserved','running','review','settled','canceled') AND "
            "reservation_state IN ('held','captured','released') AND "
            "input_limit >= 0 AND output_limit >= 0 AND refunded_amount <= "
            "COALESCE(charged_amount,0)"
        ),
        schema="billing",
    )
    op.create_index(
        "requests_org_page",
        "request_runs",
        ["organization_id", "created_at", "id"],
        schema="billing",
    )
    op.create_index(
        "requests_held_expiry",
        "request_runs",
        ["expires_at"],
        schema="billing",
        postgresql_where=sa.text("reservation_state='held'"),
    )
    op.create_table(
        "budget_holds",
        col("request_id", primary_key=True),
        col("period_id", primary_key=True),
        col("organization_id"),
        money("reserved"),
        money("spent"),
        sa.ForeignKeyConstraint(
            ["organization_id", "request_id"],
            ["billing.request_runs.organization_id", "billing.request_runs.id"],
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "period_id"],
            ["billing.budget_periods.organization_id", "billing.budget_periods.id"],
        ),
        check_money("reserved", "spent"),
        schema="billing",
    )
    op.create_table(
        "request_attempts",
        *record(),
        col("request_id"),
        col("deployment_id"),
        col("cost_price_version_id"),
        col("state", sa.String(20), server_default="started"),
        money("cost_amount", nullable=True),
        col("cost_details", JSONB(), True),
        col("usage_digest", sa.String(64), True),
        sa.ForeignKeyConstraint(["request_id"], ["billing.request_runs.id"]),
        sa.ForeignKeyConstraint(["cost_price_version_id"], ["billing.price_versions.id"]),
        check_money("cost_amount"),
        sa.CheckConstraint("state IN ('started','confirmed')"),
        schema="billing",
    )
    op.create_index("attempt_request", "request_attempts", ["request_id"], schema="billing")
    op.execute("""
        CREATE FUNCTION billing.protect_published_version() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN
            IF OLD.state = 'published' THEN
                RAISE EXCEPTION 'published version is immutable' USING ERRCODE='23514';
            END IF;
            IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
            RETURN NEW;
        END $$
    """)
    for name in ("price_versions", "discount_versions"):
        op.execute(
            f"CREATE TRIGGER {name}_immutable BEFORE UPDATE OR DELETE ON billing.{name} "
            "FOR EACH ROW EXECUTE FUNCTION billing.protect_published_version()"
        )
    op.execute("""
        CREATE FUNCTION billing.protect_admission() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN
            IF ROW(OLD.organization_id, OLD.account_id, OLD.project_id,
                OLD.service_account_id, OLD.api_key_id, OLD.model_id, OLD.request_digest,
                OLD.sale_price_version_id, OLD.discount_version_id, OLD.billing_policy_version,
                OLD.input_limit, OLD.output_limit, OLD.reserved_amount,
                OLD.created_at, OLD.expires_at)
            IS DISTINCT FROM ROW(NEW.organization_id, NEW.account_id, NEW.project_id,
                NEW.service_account_id, NEW.api_key_id, NEW.model_id, NEW.request_digest,
                NEW.sale_price_version_id, NEW.discount_version_id, NEW.billing_policy_version,
                NEW.input_limit, NEW.output_limit, NEW.reserved_amount,
                NEW.created_at, NEW.expires_at) THEN
                RAISE EXCEPTION 'request admission is immutable' USING ERRCODE='23514';
            END IF;
            IF OLD.reservation_state <> 'held' AND
                ROW(OLD.state, OLD.reservation_state, OLD.charged_amount,
                    OLD.settlement_digest, OLD.charge_details, OLD.settled_at)
                IS DISTINCT FROM
                ROW(NEW.state, NEW.reservation_state, NEW.charged_amount,
                    NEW.settlement_digest, NEW.charge_details, NEW.settled_at) THEN
                RAISE EXCEPTION 'settlement is immutable' USING ERRCODE='23514';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute(
        "CREATE TRIGGER request_admission BEFORE UPDATE ON billing.request_runs "
        "FOR EACH ROW EXECUTE FUNCTION billing.protect_admission()"
    )
    op.execute("""
        CREATE FUNCTION billing.protect_attempt() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN
            IF OLD.state='confirmed' OR
                ROW(OLD.request_id, OLD.deployment_id, OLD.cost_price_version_id)
                IS DISTINCT FROM
                ROW(NEW.request_id, NEW.deployment_id, NEW.cost_price_version_id) THEN
                RAISE EXCEPTION 'attempt evidence is immutable' USING ERRCODE='23514';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute(
        "CREATE TRIGGER attempt_immutable BEFORE UPDATE ON billing.request_attempts "
        "FOR EACH ROW EXECUTE FUNCTION billing.protect_attempt()"
    )
    for name in (
        "price_versions",
        "discount_versions",
        "budget_policies",
        "budget_periods",
        "budget_holds",
        "request_runs",
        "request_attempts",
    ):
        op.execute(f"GRANT SELECT, INSERT, UPDATE ON billing.{name} TO nexus_billing")


def downgrade():
    for name in (
        "request_attempts",
        "budget_holds",
        "request_runs",
        "budget_periods",
        "budget_policies",
        "discount_versions",
        "price_versions",
    ):
        op.drop_table(name, schema="billing")
    op.execute("DROP FUNCTION billing.protect_attempt()")
    op.execute("DROP FUNCTION billing.protect_admission()")
    op.execute("DROP FUNCTION billing.protect_published_version()")
