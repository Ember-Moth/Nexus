"""Server-owned correlation IDs and allowlisted structured process events."""

import json
import logging
from contextvars import ContextVar
from datetime import UTC, datetime
from uuid import UUID

request_id: ContextVar[UUID | None] = ContextVar("nexus_request_id", default=None)
FIELDS = frozenset(
    {
        "request_id",
        "method",
        "route",
        "status",
        "error_code",
        "error_type",
        "elapsed_ms",
        "job_id",
        "provider",
        "purpose",
        "attempt",
        "provider_status",
        "outcome",
        "consumer",
        "ready",
    }
)


def event(logger, level, name, **fields):
    if not fields.keys() <= FIELDS:
        raise ValueError("Unknown telemetry field")
    logger.log(level, name, extra={"event_fields": fields})


class JsonFormatter(logging.Formatter):
    def format(self, record):
        data = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname.lower(),
            "target": record.name,
            "message": record.getMessage(),
        }
        data.update(
            {
                k: str(v) if isinstance(v, UUID) else v
                for k, v in getattr(record, "event_fields", {}).items()
                if v is not None
            }
        )
        return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def configure_logging(level):
    # Dependency clients may log full URLs or request headers at debug level.
    # Keep our event stream independent of their verbosity.
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    app = logging.getLogger("nexus")
    app.handlers[:] = [handler]
    app.propagate = False
    app.setLevel(level.upper())
    logging.basicConfig(level=logging.WARNING)
    for name in ("httpx", "httpcore", "smtplib"):
        logging.getLogger(name).setLevel(logging.WARNING)
