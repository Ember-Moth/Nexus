"""Wire types for response views assembled from rows and stored JSON documents.

Views keep the representation a value already has on the wire. Timestamps read from
PostgreSQL JSON stay as their original RFC 3339 text and Python datetimes use ``isoformat``,
matching the encoding these responses had before they were typed. Money stays a decimal string.
"""

from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer, PlainValidator, WithJsonSchema


class View(BaseModel):
    """A response view; undeclared fields fail validation instead of leaking."""

    model_config = ConfigDict(extra="forbid", frozen=True)


def _aware_timestamp(value: Any) -> datetime | str:
    parsed = value
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            parsed = None
    if isinstance(parsed, datetime) and parsed.tzinfo is not None:
        return value
    raise ValueError("expected a timezone-aware timestamp")


type Timestamp = Annotated[
    datetime | str,
    PlainValidator(_aware_timestamp),
    PlainSerializer(
        lambda value: value.isoformat() if isinstance(value, datetime) else value,
        return_type=str,
    ),
    WithJsonSchema({"type": "string", "format": "date-time"}),
]
type DecimalString = Annotated[
    str,
    WithJsonSchema({"type": "string", "format": "decimal", "pattern": r"^-?[0-9]+(\.[0-9]+)?$"}),
]

# Byte-seconds routinely exceed the exact integer range of JavaScript clients.
ByteSeconds = Annotated[int, Field(ge=0), PlainSerializer(str, return_type=str)]


def meter_counts(values):
    return {k: str(v) if k == "vector_storage_byte_seconds" else v for k, v in values.items()}


MeterCounts = Annotated[
    dict[str, int], PlainSerializer(meter_counts, return_type=dict[str, int | str])
]


class ManagedSource(View):
    """The policy file that locked a policy group; only another import can change the group."""

    file_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    imported_at: Timestamp


def managed_source(row) -> ManagedSource | None:
    """From the ``managed_sha256`` and ``managed_at`` columns every policy row carries."""
    if row["managed_sha256"] is None:
        return None
    return ManagedSource(file_sha256=row["managed_sha256"], imported_at=row["managed_at"])
