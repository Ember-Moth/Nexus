"""Close self-service registration by default and decouple mail delivery from token expiry.

- ``auth_policy.registration`` gates self-service sign-up. It starts closed and can only be opened
  together with email verification.
- ``email_challenges.deliver_by`` bounds how long an unsent message may wait in the queue. Token
  validity now starts when the worker hands the message to the provider, so a backlog no longer
  expires verification and reset mail before it is sent.
- ``purpose='exists'`` notifies an address that a registration was attempted for a login that is
  already taken, so the registration response does not disclose it.

The purge index is built with CREATE INDEX CONCURRENTLY as the reference use of
nexus.infrastructure.migrations; the table itself is small.
"""

from alembic import op

from nexus.infrastructure.migrations import create_index_concurrently, drop_index_concurrently

revision = "0036_identity_abuse_limits"
down_revision = "0035_upstream_purges"
branch_labels = None
depends_on = None

PURGE_KEY = "((CASE WHEN ciphertext IS NULL THEN expires_at ELSE deliver_by END))"


def upgrade():
    op.execute("""ALTER TABLE identity.auth_policy
        ADD COLUMN registration boolean NOT NULL DEFAULT false,
        ADD CONSTRAINT auth_policy_registration_check CHECK (NOT registration OR email_verification)
    """)
    # The default keeps API processes that predate this revision able to queue mail during a
    # rolling upgrade; current code always supplies the deadline.
    op.execute("""ALTER TABLE identity.email_challenges
        ADD COLUMN deliver_by timestamptz NOT NULL DEFAULT now() + interval '1 hour',
        DROP CONSTRAINT email_challenges_purpose_check,
        DROP CONSTRAINT email_challenges_check,
        ADD CONSTRAINT email_challenges_purpose_check
            CHECK (purpose IN ('register','bind','reset','exists')),
        ADD CONSTRAINT email_challenges_check
            CHECK ((purpose IN ('register','exists')) = (user_id IS NULL))
    """)
    op.execute("UPDATE identity.email_challenges SET deliver_by=greatest(expires_at, now())")
    create_index_concurrently("email_purge_idx", "identity.email_challenges", PURGE_KEY)
    drop_index_concurrently("identity.email_expiry_idx")


def downgrade():
    # Downgrades run in one transaction and the table is small, so plain DDL keeps them atomic.
    op.execute("DROP INDEX identity.email_purge_idx")
    op.execute("CREATE INDEX email_expiry_idx ON identity.email_challenges(expires_at)")
    op.execute("DELETE FROM identity.email_challenges WHERE purpose='exists'")
    op.execute("""ALTER TABLE identity.email_challenges
        DROP CONSTRAINT email_challenges_purpose_check,
        DROP CONSTRAINT email_challenges_check,
        ADD CONSTRAINT email_challenges_purpose_check
            CHECK (purpose IN ('register','bind','reset')),
        ADD CONSTRAINT email_challenges_check CHECK ((purpose='register') = (user_id IS NULL)),
        DROP COLUMN deliver_by
    """)
    op.execute("""ALTER TABLE identity.auth_policy
        DROP CONSTRAINT auth_policy_registration_check, DROP COLUMN registration""")
