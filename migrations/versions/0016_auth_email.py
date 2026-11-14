"""Verified recovery addresses, authentication policy and pending email challenges."""

from alembic import op

revision = "0016_auth_email"
down_revision = "0015_personal_organizations"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE identity.users ADD COLUMN verified_email varchar(254) UNIQUE")
    op.execute("""CREATE TABLE identity.auth_policy (
        singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton),
        email_verification boolean NOT NULL DEFAULT false,
        turnstile boolean NOT NULL DEFAULT false,
        version integer NOT NULL DEFAULT 1 CHECK(version > 0)
    )""")
    op.execute("INSERT INTO identity.auth_policy DEFAULT VALUES")
    op.execute("""CREATE TABLE identity.email_challenges (
        id uuid PRIMARY KEY, purpose text NOT NULL CHECK(purpose IN ('register','bind','reset')),
        user_id uuid REFERENCES identity.users(id), email varchar(254) NOT NULL,
        login varchar(254), password_hash varchar(512),
        digest varchar(64) UNIQUE NOT NULL, expires_at timestamptz NOT NULL,
        key_version integer NOT NULL, ciphertext bytea,
        next_attempt timestamptz NOT NULL DEFAULT now(),
        attempts integer NOT NULL DEFAULT 0, lease_id uuid,
        CHECK ((purpose='register') = (user_id IS NULL)),
        CHECK ((purpose='register') = (login IS NOT NULL AND password_hash IS NOT NULL))
    )""")
    op.execute(
        "CREATE INDEX email_dispatch_idx ON identity.email_challenges(next_attempt) "
        "WHERE ciphertext IS NOT NULL"
    )
    op.execute("CREATE INDEX email_expiry_idx ON identity.email_challenges(expires_at)")
    op.execute("CREATE INDEX email_user_idx ON identity.email_challenges(user_id, purpose)")
    op.execute("GRANT SELECT, UPDATE ON identity.auth_policy TO nexus_identity")
    op.execute(
        "GRANT SELECT, INSERT, UPDATE, DELETE ON identity.email_challenges TO nexus_identity"
    )


def downgrade():
    op.execute("""DO $$ BEGIN IF EXISTS(SELECT FROM identity.users WHERE verified_email IS NOT NULL)
        OR EXISTS(SELECT FROM identity.email_challenges) OR EXISTS(SELECT FROM identity.auth_policy
        WHERE email_verification OR turnstile) THEN
            RAISE EXCEPTION 'email authentication data is retained';
        END IF; END $$""")
    op.execute("DROP TABLE identity.email_challenges, identity.auth_policy")
    op.execute("ALTER TABLE identity.users DROP COLUMN verified_email")
