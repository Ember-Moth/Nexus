"""Identity limits, credential lifetimes and reserved logins in ``identity.auth_policy``.

Sign-in and identity mail limits, session and password setup lifetimes, the logins that
self-service registration may not claim and platform email recovery join the registration and
verification switches in the singleton policy row and share its version.

``managed_sha256`` and ``managed_at`` name the policy file that locked the row through
``nexus settings import``; while they are set, only another import changes the row.
"""

from alembic import op

revision = "0039_identity_policy"
down_revision = "0038_upstream_resources"
branch_labels = None
depends_on = None

RESERVED_LOGINS = (
    "abuse",
    "admin",
    "administrator",
    "hostmaster",
    "mailer-daemon",
    "nexus",
    "no-reply",
    "noreply",
    "operator",
    "postmaster",
    "root",
    "security",
    "superuser",
    "support",
    "sysadmin",
    "system",
    "webmaster",
)
# (column, default, minimum, maximum)
LIMITS = (
    ("login_window_seconds", 60, 1, 3600),
    ("login_per_account", 10, 1, 1000),
    ("login_per_ip", 60, 1, 10000),
    ("mail_window_seconds", 3600, 60, 86400),
    ("mail_per_recipient", 3, 1, 1000),
    ("mail_per_recipient_total", 10, 1, 10000),
    ("mail_per_account", 10, 1, 10000),
    ("mail_per_ip", 20, 1, 100000),
    ("mail_global", 1000, 1, 10_000_000),
    ("mail_queue_ttl_seconds", 3600, 60, 86400),
    ("session_ttl_seconds", 28800, 60, 86400),
    ("password_setup_ttl_seconds", 86400, 60, 86400),
)


# Every policy row records the file that locked it, if any.
MANAGED = (
    "ADD COLUMN managed_sha256 text CHECK(managed_sha256 ~ '^[0-9a-f]{64}$')",
    "ADD COLUMN managed_at timestamptz",
    "ADD CONSTRAINT auth_policy_managed_check "
    "CHECK((managed_sha256 IS NULL) = (managed_at IS NULL))",
)


def upgrade():
    reserved = ",".join(f"'{login}'" for login in RESERVED_LOGINS)
    columns = [
        f"ADD COLUMN {name} integer NOT NULL DEFAULT {default} "
        f"CONSTRAINT auth_policy_{name}_check CHECK({name} BETWEEN {low} AND {high})"
        for name, default, low, high in LIMITS
    ]
    columns += [
        f"ADD COLUMN reserved_logins text[] NOT NULL DEFAULT ARRAY[{reserved}]::text[] "
        "CONSTRAINT auth_policy_reserved_logins_check CHECK(cardinality(reserved_logins)<=1000 "
        "AND array_position(reserved_logins, NULL) IS NULL)",
        "ADD COLUMN platform_email_recovery boolean NOT NULL DEFAULT false",
        "ADD CONSTRAINT auth_policy_mail_recipient_check "
        "CHECK(mail_per_recipient <= mail_per_recipient_total)",
        *MANAGED,
    ]
    op.execute("ALTER TABLE identity.auth_policy " + ", ".join(columns))


def downgrade():
    names = [name for name, *_ in LIMITS]
    names += ["reserved_logins", "platform_email_recovery", "managed_sha256", "managed_at"]
    op.execute(
        "ALTER TABLE identity.auth_policy DROP CONSTRAINT auth_policy_mail_recipient_check, "
        + ", ".join(f"DROP COLUMN {name}" for name in names)
    )
