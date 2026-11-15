"""Login names: one canonical spelling, no hidden characters, no squatting of operator names.

New logins are stored in their canonical form (NFKC, trimmed, lower-case). Accounts created before
that rule kept ``strip().lower()`` of whatever was typed, so sign-in also accepts that spelling;
the strict checks apply only when a login is created.
"""

import unicodedata

from .errors import IdentityError

# Unicode Default_Ignorable_Code_Point ranges, plus the Braille blank, which renders as nothing.
_INVISIBLE = (
    (0x00AD, 0x00AD),
    (0x034F, 0x034F),
    (0x061C, 0x061C),
    (0x115F, 0x1160),
    (0x17B4, 0x17B5),
    (0x180B, 0x180F),
    (0x200B, 0x200F),
    (0x202A, 0x202E),
    (0x2060, 0x206F),
    (0x2800, 0x2800),
    (0x3164, 0x3164),
    (0xFE00, 0xFE0F),
    (0xFEFF, 0xFEFF),
    (0xFFA0, 0xFFA0),
    (0xFFF0, 0xFFF8),
    (0x1BCA0, 0x1BCA3),
    (0x1D173, 0x1D17A),
    (0xE0000, 0xE0FFF),
)


def _invalid(message: str) -> IdentityError:
    return IdentityError("invalid_login", message, 422)


def _shape(login: str) -> str:
    if not 3 <= len(login) <= 254 or any(char.isspace() for char in login):
        raise _invalid("Login must contain 3-254 non-whitespace characters")
    return login


def _visible(char: str) -> bool:
    # C*: control, format, surrogate, private use, unassigned; Z*: separators.
    if unicodedata.category(char)[0] in "CZ":
        return False
    code = ord(char)
    return not any(low <= code <= high for low, high in _INVISIBLE)


def canonical_login(value: str) -> str:
    """The spelling an account is found and rate-limited under."""
    folded = unicodedata.normalize("NFKC", value).strip().lower()
    # Lower-casing can leave a compatibility sequence behind, so normalize once more.
    return _shape(unicodedata.normalize("NFKC", folded))


def login_candidates(value: str) -> tuple[str, ...]:
    """Stored spellings that may belong to this input, canonical first."""
    canonical = canonical_login(value)
    legacy = value.strip().lower()
    if legacy == canonical:
        return (canonical,)
    try:
        return canonical, _shape(legacy)
    except IdentityError:
        return (canonical,)


def new_login(value: str, reserved: frozenset[str] = frozenset()) -> str:
    """Validate a login that is about to be created."""
    login = canonical_login(value)
    if not all(_visible(char) for char in login):
        raise _invalid("Login cannot contain control or invisible characters")
    if login in reserved:
        raise _invalid("This login is reserved")
    return login
