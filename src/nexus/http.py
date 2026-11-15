"""HTTP parsing helpers and the shared error contract, independent of business modules."""

import base64
import binascii
import hashlib
import hmac
import json
import re
from datetime import datetime
from ipaddress import IPv4Address, IPv6Address, ip_address, ip_network
from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends, Header, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, SecretStr

from nexus.shared.error_catalog import CATALOG, error_catalog
from nexus.shared.errors import ServiceError

bearer = HTTPBearer(auto_error=False)
SCHEMA_REF = "#/components/schemas/{model}"


class ErrorBody(BaseModel):
    code: str = Field(description="Stable machine-readable error code")
    message: str
    trace_id: str = Field(description="Matches the X-Request-ID response header")


class ErrorResponse(BaseModel):
    """The envelope of every management API error."""

    error: ErrorBody


class FieldError(BaseModel):
    location: list[str | int]
    code: str


class ValidationErrorBody(ErrorBody):
    fields: list[FieldError] | None = Field(
        default=None,
        description="Rejected inputs, present when the request itself could not be parsed",
    )


class ValidationErrorResponse(BaseModel):
    error: ValidationErrorBody


ERROR_DESCRIPTIONS = {
    401: "Session is missing, expired or revoked",
    403: "Caller may not perform this operation",
    404: "Resource does not exist or is not visible to the caller",
    409: "Idempotency key reuse, state transition or resource conflict",
    410: "Invitation has expired",
    412: "If-Match does not match the current resource version",
    422: "Request is malformed or violates a business rule",
    428: "A required If-Match or Idempotency-Key header is missing",
    429: "Rate limit reached; retry later",
    500: "Internal server error",
    503: "A storage or coordination dependency is unavailable",
}


def error_responses(*statuses: int) -> dict[int | str, dict[str, Any]]:
    """Declare operation-specific errors; protocol errors are derived from the operation."""
    return {
        status: {
            "model": ValidationErrorResponse if status == 422 else ErrorResponse,
            "description": ERROR_DESCRIPTIONS[status],
        }
        for status in statuses
    }


def business_errors(*codes: str, challenge=False):
    """Declare client-actionable codes on a route; shared transport errors are inferred."""
    known = set(codes)
    if challenge:
        known.update(
            {
                "authentication_unavailable",
                "auth_policy_changed",
                "rate_limited",
                "turnstile_required",
                "turnstile_rejected",
                "turnstile_unavailable",
            }
        )
    by_status = {}
    for code in known:
        matches = [status for name, status in CATALOG if name == code]
        if not matches:
            raise ValueError(f"Unregistered error code: {code}")
        for status in matches:
            by_status.setdefault(status, []).append(code)
    responses = error_responses(*by_status)
    for status, options in by_status.items():
        responses[status]["x-error-codes"] = sorted(options)
    return responses


def _error_statuses(operation: dict[str, Any]) -> set[int]:
    parameters = operation.get("parameters", [])
    headers = {p["name"].lower() for p in parameters if p["in"] == "header"}
    # Every operation authenticates or reads through PostgreSQL or Redis.
    statuses = {500, 503}
    if operation.get("security"):
        statuses |= {401, 403}
    if "if-match" in headers:
        statuses |= {412, 428}
    if "idempotency-key" in headers:
        statuses |= {409, 428}
    if any(p["in"] == "path" for p in parameters):
        statuses.add(404)
    if parameters or "requestBody" in operation:
        statuses.add(422)
    return statuses


def declare_error_responses(openapi: dict[str, Any]) -> dict[str, Any]:
    """Add the error envelope for the failures each operation's inputs imply.

    FastAPI's default 422 schema describes its own validation body, which the API replaces with
    the shared envelope, so 422 is always rewritten.
    """
    schemas = openapi.setdefault("components", {}).setdefault("schemas", {})
    for model in (ErrorResponse, ValidationErrorResponse):
        schema = model.model_json_schema(ref_template=SCHEMA_REF, mode="serialization")
        for name, definition in schema.pop("$defs", {}).items():
            schemas.setdefault(name, definition)
        schemas.setdefault(model.__name__, schema)
    openapi["x-error-catalog"] = {
        code: [guide.model_dump() for guide in guides] for code, guides in error_catalog().items()
    }
    for item in openapi.get("paths", {}).values():
        for method, operation in item.items():
            if method not in {"get", "put", "post", "patch", "delete"}:
                continue
            responses = operation.setdefault("responses", {})
            for status in _error_statuses(operation):
                if status == 422 and str(status) in responses:
                    responses[str(status)]["description"] = ERROR_DESCRIPTIONS[status]
                if str(status) not in responses:
                    responses[str(status)] = {"description": ERROR_DESCRIPTIONS[status]}
            shared = {"internal_error", "validation_error", "storage_unavailable"}
            if operation.get("security"):
                shared.update({"unauthorized", "forbidden"})
            for parameter in operation.get("parameters", []):
                if parameter["in"] == "path":
                    shared.add("not_found")
                if parameter["name"] == "cursor":
                    shared.add("invalid_cursor")
                if parameter["name"] == "If-Match":
                    shared.update({"invalid_version", "precondition_required", "version_conflict"})
                if parameter["name"] == "Idempotency-Key":
                    shared.update(
                        {"invalid_idempotency_key", "precondition_required", "idempotency_conflict"}
                    )
            for code, status in CATALOG:
                if code in shared and str(status) in responses:
                    response = responses[str(status)]
                    response["x-error-codes"] = sorted({code, *response.get("x-error-codes", [])})
            for status, response in responses.items():
                if status.isdigit() and int(status) >= 400:
                    # Errors are JSON even where the success body is a file.
                    model = ValidationErrorResponse if status == "422" else ErrorResponse
                    ref = {"$ref": SCHEMA_REF.format(model=model.__name__)}
                    if status == "429":
                        response.setdefault("headers", {})["Retry-After"] = {
                            "description": "When supplied, seconds to wait before retrying",
                            "schema": {"type": "integer", "minimum": 1},
                        }
                    response["content"] = {"application/json": {"schema": ref}}
            operation["responses"] = dict(sorted(responses.items()))
    # Drop FastAPI's validation schemas once nothing references them.
    for name in ("HTTPValidationError", "ValidationError"):
        schemas.pop(name, None)
        if json.dumps(SCHEMA_REF.format(model=name)) in json.dumps(openapi):
            raise RuntimeError(f"{name} is still referenced by the OpenAPI document")
    return openapi


def credentials(
    request: Request, value: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]
) -> str:
    if value is None or len(request.headers.getlist("authorization")) != 1:
        raise ServiceError("unauthorized", "Invalid or expired credentials", 401)
    return value.credentials


def expected_version(if_match: Annotated[str | None, Header(alias="If-Match")] = None) -> int:
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


def key_cursor_value(cursor: str | None, size: int) -> tuple[UUID, ...] | None:
    """Decode a cursor for lists ordered by `size` UUID key columns."""
    if cursor is None:
        return None
    try:
        values = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
        if not isinstance(values, list) or len(values) != size:
            raise ValueError
        return tuple(UUID(value) for value in values)
    except ValueError, TypeError, AttributeError, binascii.Error:
        raise ServiceError("invalid_cursor", "Invalid pagination cursor", 422) from None


def idempotency_key(value: Annotated[str | None, Header(alias="Idempotency-Key")] = None) -> str:
    if value is None:
        raise ServiceError("precondition_required", "Idempotency-Key is required", 428)
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value):
        raise ServiceError("invalid_idempotency_key", "Invalid Idempotency-Key", 422)
    return value


def optional_version(
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> int | None:
    return expected_version(if_match) if if_match is not None else None


CLIENT_IP_HEADER = "X-Nexus-Client-IP"
CLIENT_IP_SIGNATURE_HEADER = "X-Nexus-Client-IP-Signature"


def client_ip_signature(secret: str, address: str) -> str:
    """HMAC-SHA256 the BFF attaches to the exact client address header value it forwards."""
    digest = hmac.new(
        secret.encode(), b"nexus-client-ip:v1:" + address.encode(), hashlib.sha256
    ).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def limit_bucket(address: str) -> str:
    """Rate-limit key for a client address: IPv4 as is, IPv6 aggregated to its /64."""
    try:
        parsed = ip_address(address)
    except ValueError:
        return address
    if isinstance(parsed, IPv6Address):
        if parsed.ipv4_mapped:
            return str(parsed.ipv4_mapped)
        # One subscriber normally receives a whole /64, so a single host can rotate through
        # 2**64 addresses; counting them separately would void every per-IP limit.
        return str(ip_network(f"{parsed}/64", strict=False))
    return str(parsed)


def auth_limit_peer(
    request: Request,
    trusted: frozenset[IPv4Address | IPv6Address],
    secret: SecretStr | None = None,
) -> str:
    """Client address bucket for authentication limits.

    A forwarded client address is accepted only from a configured BFF peer and only when it carries
    the BFF's signature: a reverse proxy on the same host as the BFF may relay a header supplied
    by the client, so the peer address alone does not prove that the BFF set it.
    """
    direct = request.client.host if request.client else "unknown"
    if not trusted or secret is None:
        return limit_bucket(direct)
    try:
        peer = ip_address(direct)
    except ValueError:
        return direct
    if peer not in trusted:
        return limit_bucket(direct)
    values = request.headers.getlist(CLIENT_IP_HEADER)
    signatures = request.headers.getlist(CLIENT_IP_SIGNATURE_HEADER)
    if len(values) == 1 and len(signatures) == 1 and "%" not in values[0]:
        expected = client_ip_signature(secret.get_secret_value(), values[0])
        if hmac.compare_digest(expected.encode(), signatures[0].encode()):
            try:
                ip_address(values[0])
                return limit_bucket(values[0])
            except ValueError:
                pass
    return limit_bucket(direct)
