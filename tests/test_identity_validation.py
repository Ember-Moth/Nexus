import base64
import json

import pytest

from nexus.http import cursor_value
from nexus.shared.errors import ServiceError


@pytest.mark.parametrize(
    "value",
    [
        ["2026-11-13T00:00:00Z", 123],
        [123, "00000000-0000-4000-8000-000000000001"],
        ["2026-11-13T00:00:00", "00000000-0000-4000-8000-000000000001"],
        {"position": 0},
    ],
)
def test_malformed_cursor_is_a_client_error(value):
    cursor = base64.urlsafe_b64encode(json.dumps(value).encode()).decode()
    with pytest.raises(ServiceError) as raised:
        cursor_value(cursor)
    assert raised.value.status == 422


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("  Alice ", "alice"),
        ("ＡＬＩＣＥ", "alice"),  # full-width compatibility characters
        ("ﬁnance-team", "finance-team"),  # ligature
        ("alice@ＥＸＡＭＰＬＥ.com", "alice@example.com"),
    ],
)
def test_new_logins_are_stored_in_nfkc_form(value, expected):
    from nexus.modules.identity.domain.logins import new_login

    assert new_login(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "ali​ce",  # zero-width space
        "ali‍ce",  # zero-width joiner
        "alice‮",  # right-to-left override
        "ali\x07ce",  # control character
        "aliㅤce",  # Hangul filler renders blank
        "ali­ce",  # soft hyphen
        "ali️ce",  # variation selector
        "ab",
        "two words",
        "ideographic　space",
    ],
)
def test_new_logins_reject_hidden_and_malformed_characters(value):
    from nexus.modules.identity.domain.errors import IdentityError
    from nexus.modules.identity.domain.logins import new_login

    with pytest.raises(IdentityError) as rejected:
        new_login(value)
    assert rejected.value.code == "invalid_login" and rejected.value.status == 422


@pytest.mark.parametrize("value", ["admin", "ADMIN", "ａｄｍｉｎ", " Root "])
def test_reserved_logins_cover_every_spelling(value):
    from nexus.modules.identity.application.schemas import normalize_logins
    from nexus.modules.identity.domain.errors import IdentityError
    from nexus.modules.identity.domain.logins import new_login

    reserved = frozenset(normalize_logins(["Admin", "root", "system"]))
    with pytest.raises(IdentityError):
        new_login(value, reserved)
    assert new_login("admin-team", reserved) == "admin-team"


def test_sign_in_looks_up_the_canonical_login():
    from nexus.modules.identity.domain.logins import canonical_login

    assert canonical_login("ＪＯＨＮ") == canonical_login(" John ") == "john"


def test_configured_reserved_logins_are_normalized():
    from pydantic import ValidationError

    from nexus.modules.identity.application.schemas import AuthPolicy, normalize_logins

    assert normalize_logins(["ＯＰＳ", "Billing ", "ops"]) == ["billing", "ops"]
    field = AuthPolicy.model_fields["reserved_logins"]
    assert field.is_required()
    with pytest.raises(ValueError):
        normalize_logins(["  "])
    with pytest.raises(ValidationError):
        AuthPolicy.model_validate({"email_verification": False, "turnstile": False})
