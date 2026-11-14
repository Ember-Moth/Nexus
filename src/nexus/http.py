"""HTTP parsing helpers and the shared error contract, independent of business modules."""

import base64
import binascii
import json
import re
from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends, Header, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

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
    412: "If-Match does not match the current resource version",
    422: "Request is malformed or violates a business rule",
    428: "A required If-Match or Idempotency-Key header is missing",
    429: "Rate limit reached; retry later",
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


def _error_statuses(operation: dict[str, Any]) -> set[int]:
    parameters = operation.get("parameters", [])
    headers = {p["name"].lower() for p in parameters if p["in"] == "header"}
    # Every operation authenticates or reads through PostgreSQL or Redis.
    statuses = {503}
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
    for item in openapi.get("paths", {}).values():
        for method, operation in item.items():
            if method not in {"get", "put", "post", "patch", "delete"}:
                continue
            responses = operation.setdefault("responses", {})
            for status in _error_statuses(operation):
                if status == 422 or str(status) not in responses:
                    responses[str(status)] = {"description": ERROR_DESCRIPTIONS[status]}
            for status, response in responses.items():
                if status.isdigit() and int(status) >= 400:
                    # Errors are JSON even where the success body is a file.
                    model = ValidationErrorResponse if status == "422" else ErrorResponse
                    ref = {"$ref": SCHEMA_REF.format(model=model.__name__)}
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
