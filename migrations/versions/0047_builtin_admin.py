"""The built-in administrator, its setup wizard state and the site description.

- ``identity.users.unverified_email``: an address the account signs in with but has not proven.
  An account has at most one address, verified or not; proving one moves it to
  ``verified_email``.
- The built-in administrator (login ``admin``, email ``admin@example.com``, password
  ``password``) holds ``platform_admin``. Its password is stored as a precomputed Argon2id hash.
- ``identity.site_policy`` (singleton, typed policy like ``identity.auth_policy``): site name,
  public console address and contact email, plus ``setup_completed_at``. Until the setup wizard
  sets it, the management API refuses every call except sign-in and the wizard.
  ``nexus setup reset`` clears it again.

A reinstall (empty database, then ``migrate``) always starts from this state.
"""

from alembic import op

revision = "0047_builtin_admin"
down_revision = "0046_export_objects"
branch_labels = None
depends_on = None

ADMIN_ID = "00000000-0000-4000-8000-000000000001"
ADMIN_LOGIN = "admin"
ADMIN_EMAIL = "admin@example.com"
# Argon2id (m=65536, t=3, p=4) of "password".
ADMIN_PASSWORD_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=4$yzLccdji9Va1kKs1NYFVnQ$"
    "Azl8a7QXycjkcYPyGwz9gIVuLqFtwLHlTObS0HVofSA"
)


def upgrade():
    op.execute("""ALTER TABLE identity.users
        ADD COLUMN unverified_email varchar(254) UNIQUE,
        ADD CONSTRAINT users_single_email
            CHECK(verified_email IS NULL OR unverified_email IS NULL)""")
    op.execute("""CREATE TABLE identity.site_policy (
        singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton),
        site_name text NOT NULL DEFAULT 'Nexus' CHECK(char_length(site_name) BETWEEN 1 AND 100),
        console_url text CHECK(console_url ~ '^https?://' AND char_length(console_url) <= 2048),
        contact_email varchar(254),
        setup_completed_at timestamptz,
        version integer NOT NULL DEFAULT 1 CHECK(version > 0),
        managed_sha256 text CHECK(managed_sha256 ~ '^[0-9a-f]{64}$'),
        managed_at timestamptz,
        CHECK((managed_sha256 IS NULL) = (managed_at IS NULL))
    )""")
    op.execute("INSERT INTO identity.site_policy DEFAULT VALUES")
    op.execute("GRANT SELECT, UPDATE ON identity.site_policy TO nexus_identity")
    op.execute(f"""DO $$ BEGIN
        IF EXISTS(SELECT FROM identity.users WHERE login='{ADMIN_LOGIN}' AND id<>'{ADMIN_ID}')
        THEN RAISE EXCEPTION
            'login "{ADMIN_LOGIN}" belongs to another account; reinstall on an empty database';
        END IF; END $$""")
    # A downgrade keeps the account, so an upgrade after it finds the same row.
    op.execute(f"""INSERT INTO identity.users(id, login, password_hash, status, unverified_email)
        VALUES('{ADMIN_ID}', '{ADMIN_LOGIN}', '{ADMIN_PASSWORD_HASH}', 'active', '{ADMIN_EMAIL}')
        ON CONFLICT (id) DO UPDATE SET unverified_email='{ADMIN_EMAIL}'
        WHERE identity.users.verified_email IS NULL""")
    op.execute(f"""INSERT INTO identity.platform_role_bindings(user_id, role)
        VALUES('{ADMIN_ID}', 'platform_admin') ON CONFLICT DO NOTHING""")


def downgrade():
    # The built-in administrator stays as an ordinary platform administrator.
    op.execute("DROP TABLE identity.site_policy")
    op.execute(
        "ALTER TABLE identity.users DROP CONSTRAINT users_single_email, "
        "DROP COLUMN unverified_email"
    )
