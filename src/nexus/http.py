"""HTTP parsing helpers, independent of business modules."""

import base64
import binascii
import json
import re
from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Header, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from nexus.shared.errors import ServiceError

bearer = HTTPBearer(auto_error=False)


def credentials(
    request: Request, value: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]
) -> str:
    if value is None or len(request.headers.getlist("authorization")) != 1:
        raise ServiceError("unauthorized", "Invalid or expired credentials", 401)
    return value.credentials


def expected_version(if_match: Annotated[str | None, Header()] = None) -> int:
    if if_match is None:
        raise ServiceError("precondition_required", "If-Match is required", 428)
    if not re.fullmatch(r'"[1-9][0-9]{0,9}"', if_match):
        raise ServiceError(
            "invalid_version", "If-Match must contain a quoted resource version", 422
        )
    return int(if_match[1:-1])


def cursor_value(cursor: str | None):
    if cursor is None:
        return None
    try:
        values = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
        if (
            not isinstance(values, list)
            or len(values) != 2
            or not all(isinstance(value, str) for value in values)
        ):
            raise ValueError
        at = datetime.fromisoformat(values[0])
        if at.tzinfo is None:
            raise ValueError
        return at, UUID(values[1])
    except ValueError, TypeError, binascii.Error:
        raise ServiceError("invalid_cursor", "Invalid pagination cursor", 422) from None
