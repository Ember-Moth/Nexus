import hashlib
import json

from nexus.shared.errors import ServiceError


def found(value):
    if value is None:
        raise ServiceError("not_found", "Resource not found", 404)
    return value


def versioned(row, version):
    if row.version != version:
        raise ServiceError("version_conflict", "Resource version has changed", 412)


def view(kind, row, **extra):
    return kind(
        **{name: getattr(row, name) for name in kind.model_fields if name not in extra}, **extra
    )


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


async def replay(repo, actor, scope, key, payload):
    key_digest, request_digest = digest(key), digest(payload)
    row = await repo.replay(actor, scope, key_digest)
    if row and row.request_digest != request_digest:
        raise ServiceError(
            "idempotency_conflict", "Idempotency-Key was used with different parameters", 409
        )
    return row, key_digest, request_digest
