"""The built-in administrator's first-run setup, as integration tests perform it."""

from mail_stub import PASSWORD

from nexus.modules.identity.domain.setup import (
    BUILTIN_ADMIN_ID,
    BUILTIN_ADMIN_LOGIN,
    DEFAULT_ADMIN_EMAIL,
    DEFAULT_ADMIN_PASSWORD,
    DEFAULT_ADMIN_PASSWORD_HASH,
)

API = "/api/v1"
# Where the administrator ends up after the wizard; signs in as "admin" or with this address.
ADMIN_EMAIL = "root-admin@corp.internal"
SITE = {
    "site_name": "Nexus",
    "console_url": "https://console.nexus.test",
    "contact_email": "operators@corp.internal",
}


def restore_builtin_admin(admin):
    """Return the built-in administrator to its migration state after the tables are emptied."""
    admin.execute(
        "INSERT INTO identity.users(id,login,password_hash,status,unverified_email) "
        "VALUES (%s,%s,%s,'active',%s)",
        (BUILTIN_ADMIN_ID, BUILTIN_ADMIN_LOGIN, DEFAULT_ADMIN_PASSWORD_HASH, DEFAULT_ADMIN_EMAIL),
    )
    admin.execute(
        "INSERT INTO identity.platform_role_bindings(user_id,role) VALUES (%s,'platform_admin')",
        (BUILTIN_ADMIN_ID,),
    )
    admin.execute(
        "UPDATE identity.site_policy SET site_name=DEFAULT,console_url=DEFAULT,"
        "contact_email=DEFAULT,setup_completed_at=NULL,version=1,managed_sha256=NULL,"
        "managed_at=NULL"
    )


async def sign_in(client, login=DEFAULT_ADMIN_EMAIL, password=DEFAULT_ADMIN_PASSWORD):
    response = await client.post(
        API + "/auth/sessions", json={"login": login, "password": password}
    )
    assert response.status_code == 201, response.text
    return response.json()["access_token"]


async def complete_setup(client, *, password=PASSWORD, email=ADMIN_EMAIL, site=SITE, token=None):
    """Sign in with the default credentials and finish the wizard; returns that session.

    Without mail delivery the new address is stored unverified, so no token is needed.
    """
    token = token or await sign_in(client)
    done = await client.post(
        API + "/setup",
        headers={"Authorization": f"Bearer {token}"},
        json={"site": site, "email": email, "password": password},
    )
    assert done.status_code == 200, done.text
    assert done.json()["required"] is False
    return token


async def admin_session(client):
    """A session of the built-in administrator; completes the wizard first if it is pending.

    Fixtures that share one database (``http`` and ``payments``) can both ask for it.
    """
    default = await client.post(
        API + "/auth/sessions",
        json={"login": DEFAULT_ADMIN_EMAIL, "password": DEFAULT_ADMIN_PASSWORD},
    )
    if default.status_code == 201:
        return await complete_setup(client, token=default.json()["access_token"])
    # The wizard already ran in this database.
    return await sign_in(client, BUILTIN_ADMIN_LOGIN, PASSWORD)
