"""The setup lock, the built-in administrator's defaults and the site description rules."""

import importlib.util
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from argon2 import PasswordHasher
from fastapi.routing import APIRoute, iter_route_contexts
from pydantic import ValidationError

from nexus.api import SETUP_OPEN_ROUTES, create_app, setup_open
from nexus.cli import parser
from nexus.modules.identity.application.schemas import SetupCompletion, SiteInfo
from nexus.modules.identity.domain.errors import IdentityError
from nexus.modules.identity.domain.setup import (
    BUILTIN_ADMIN_ID,
    BUILTIN_ADMIN_LOGIN,
    DEFAULT_ADMIN_EMAIL,
    DEFAULT_ADMIN_PASSWORD,
    DEFAULT_ADMIN_PASSWORD_HASH,
    check_admin_email,
    check_admin_password,
    setup_required,
)

ROOT = Path(__file__).resolve().parents[1]
BODIES = {"POST", "PUT", "PATCH"}


class Locked:
    async def require_setup_complete(self):
        raise setup_required()


def concrete(template):
    return "/".join(
        ("x" if ":path}" in part else str(uuid4())) if part.startswith("{") else part
        for part in template.split("/")
    )


def operations(app):
    """(method, path) of every API route, routers included."""
    return {
        (method, context.path)
        for context in iter_route_contexts(app.routes)
        if isinstance(context.original_route, APIRoute)
        for method in context.methods - {"HEAD", "OPTIONS"}
    }


async def test_every_route_but_sign_in_and_the_wizard_waits_for_setup(settings_factory):
    app = create_app(settings_factory())
    app.state.identity_service = Locked()
    every = operations(app)
    opened = {operation for operation in every if setup_open(*operation)}
    # Only the listed routes and provider notifications skip the lock.
    assert opened - SETUP_OPEN_ROUTES == {op for op in every if op[1].startswith("/webhooks/")}
    assert SETUP_OPEN_ROUTES <= every
    # FastAPI's own documentation routes are the only ones outside the dependency.
    assert {
        context.path
        for context in iter_route_contexts(app.routes)
        if not isinstance(context.original_route, APIRoute)
    } == {"/openapi.json", "/docs", "/docs/oauth2-redirect"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        for method, path in sorted(every):
            response = await client.request(
                method,
                concrete(path),
                headers={"Authorization": "Bearer nxs_unverified"},
                json={} if method in BODIES else None,
            )
            try:
                code = response.json()["error"]["code"]
            except ValueError, KeyError, TypeError:
                code = None
            if (method, path) in opened:
                # Open routes reach their handlers (which fail here: there is no database).
                assert code != "setup_required", (method, path)
            else:
                assert (response.status_code, code) == (409, "setup_required"), (method, path)
        # Locked before authentication: a missing session does not change the answer.
        anonymous = await client.get("/api/v1/organizations")
        assert anonymous.json()["error"]["code"] == "setup_required"


def test_contract_declares_the_lock_on_every_locked_operation(settings_factory):
    spec = create_app(settings_factory()).openapi()
    for path, item in spec["paths"].items():
        for method, operation in item.items():
            codes = operation["responses"].get("409", {}).get("x-error-codes", [])
            assert ("setup_required" in codes) != setup_open(method.upper(), path), (method, path)
    assert spec["x-error-catalog"]["setup_required"] == [
        {"status": 409, "action": "complete_setup", "retry": "after_correction"}
    ]


def test_default_account_matches_the_migration_and_the_published_password():
    assert PasswordHasher().verify(DEFAULT_ADMIN_PASSWORD_HASH, DEFAULT_ADMIN_PASSWORD)
    path = ROOT / "migrations/versions/0047_builtin_admin.py"
    spec = importlib.util.spec_from_file_location("builtin_admin_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    assert (
        migration.ADMIN_ID,
        migration.ADMIN_LOGIN,
        migration.ADMIN_EMAIL,
        migration.ADMIN_PASSWORD_HASH,
    ) == (
        str(BUILTIN_ADMIN_ID),
        BUILTIN_ADMIN_LOGIN,
        DEFAULT_ADMIN_EMAIL,
        DEFAULT_ADMIN_PASSWORD_HASH,
    )


@pytest.mark.parametrize(
    "address",
    [
        DEFAULT_ADMIN_EMAIL,
        "root@example.com",
        "root@mail.example.net",
        "root@example.org",
        "root@team.example",
    ],
)
def test_the_new_administrator_address_cannot_be_a_documentation_one(address):
    with pytest.raises(IdentityError) as refused:
        check_admin_email(address)
    assert refused.value.code == "email_reserved"


def test_real_addresses_and_passwords_pass():
    assert check_admin_email("ops@corp.internal") == "ops@corp.internal"
    assert check_admin_email("ops@notexample.com") == "ops@notexample.com"
    check_admin_password("x" * 16)
    with pytest.raises(IdentityError):
        check_admin_password(DEFAULT_ADMIN_PASSWORD)
    with pytest.raises(ValidationError):
        SetupCompletion(site={"site_name": "n"}, email="a@b.c", password=DEFAULT_ADMIN_PASSWORD)


@pytest.mark.parametrize(
    ("console_url", "stored"),
    [
        ("https://console.example.com/", "https://console.example.com"),
        ("https://console.example.com/nexus", "https://console.example.com/nexus"),
        ("http://localhost:3000", "http://localhost:3000"),
        ("http://127.0.0.1:3000/", "http://127.0.0.1:3000"),
        ("http://[::1]:3000", "http://[::1]:3000"),
        ("http://console.localhost", "http://console.localhost"),
        (None, None),
    ],
)
def test_console_addresses(console_url, stored):
    assert SiteInfo(site_name="Nexus", console_url=console_url).console_url == stored


@pytest.mark.parametrize(
    "console_url",
    [
        "http://console.example.com",
        "ftp://console.example.com",
        "https://user:secret@console.example.com",
        "https://console.example.com/?next=/",
        "https://console.example.com/#top",
        "https://console example.com",
        "https://console.example.com:99999",
        "console.example.com",
    ],
)
def test_console_address_rejections(console_url):
    with pytest.raises(ValidationError):
        SiteInfo(site_name="Nexus", console_url=console_url)


def test_site_name_and_contact():
    site = SiteInfo(site_name="  Acme AI  ", contact_email="Ops@Corp.Internal")
    assert (site.site_name, site.contact_email) == ("Acme AI", "ops@corp.internal")
    for invalid in ({"site_name": " "}, {"site_name": "a\nb"}, {"contact_email": "nobody"}):
        with pytest.raises(ValidationError):
            SiteInfo.model_validate({"site_name": "Nexus", **invalid})
    with pytest.raises(ValidationError):
        SiteInfo.model_validate({"site_name": "Nexus", "setup_completed_at": None})


def test_cli_offers_setup_reset_instead_of_bootstrap():
    arguments = parser().parse_args(["setup", "reset", "--yes"])
    assert (arguments.command, arguments.setup_command, arguments.yes) == ("setup", "reset", True)
    with pytest.raises(SystemExit):
        parser().parse_args(["bootstrap-admin", "--login", "admin"])
