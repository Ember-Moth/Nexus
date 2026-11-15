"""Operator-tunable policy rows: process-local copies and imports from a policy file.

Policies live in their owning module's tables. API and worker processes read them through a
short-lived copy: every process re-reads a policy at least once per ``POLICY_TTL_SECONDS``, so a
change committed by any process applies everywhere within that time without a restart. The
process that made a change installs the new value at once.

A policy file (``nexus settings import --file``) addresses each policy group by name. Every
module plans its group first; nothing is written unless every group of the file is valid.

- ``managed``: the file's values replace the listed fields and the group is locked. Management
  API writes are refused with ``policy_managed_externally`` until a later import unlocks it.
- ``initial``: the file's values are written only while the group has never been changed
  (version 1); the group is unlocked and administrators maintain it from then on.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from time import monotonic

from pydantic import ValidationError

POLICY_TTL_SECONDS = 2.0
IMPORT_ACTOR = "config-import"
MODES = ("initial", "managed")


class CachedPolicy[T]:
    def __init__(self, load: Callable[[], Awaitable[T]], ttl: float = POLICY_TTL_SECONDS):
        self._load = load
        self._ttl = ttl
        self._value: T | None = None
        self._expires = 0.0
        self._lock = asyncio.Lock()

    def _fresh(self) -> bool:
        return self._value is not None and monotonic() < self._expires

    async def get(self) -> T:
        if self._fresh():
            return self._value  # type: ignore[return-value]
        # One reader per process refreshes an expired copy; the others wait for its result.
        async with self._lock:
            if not self._fresh():
                self.put(await self._load())
            return self._value  # type: ignore[return-value]

    def put(self, value: T) -> None:
        self._value = value
        self._expires = monotonic() + self._ttl

    def invalidate(self) -> None:
        self._expires = 0.0


class PolicyRejected(ValueError):
    """A policy file entry that cannot be applied; names the group and fields, never values."""

    def __init__(self, group: str, reason: str):
        super().__init__(f"{group}: {reason}")
        self.group = group


def merged(current: dict, values: dict) -> dict:
    """Overlay partial values on a policy; nested groups are merged field by field."""
    result = dict(current)
    for name, value in values.items():
        if isinstance(value, dict) and isinstance(current.get(name), dict):
            result[name] = merged(current[name], value)
        else:
            result[name] = value
    return result


def flat(value: dict, prefix: str = "") -> dict:
    result = {}
    for name, item in value.items():
        if isinstance(item, dict):
            result.update(flat(item, f"{prefix}{name}."))
        else:
            result[prefix + name] = item
    return result


def changes(before: dict, after: dict) -> dict:
    """Changed fields as {"group.field": [before, after]}."""
    old, new = flat(before), flat(after)
    return {name: [old.get(name), value] for name, value in new.items() if old.get(name) != value}


def validated(group: str, schema, value: dict):
    """Validate a planned policy; field paths are reported, input values never are."""
    try:
        return schema.model_validate(value)
    except ValidationError as error:
        fields = sorted({".".join(map(str, e["loc"])) or "values" for e in error.errors()})
        raise PolicyRejected(group, "invalid " + ", ".join(fields)) from None


@dataclass
class ImportPlan:
    """What importing one group would change; ``write`` applies it with an audit record."""

    group: str
    mode: str
    managed: bool
    changes: dict = field(default_factory=dict)
    skipped: bool = False
    write: Callable[[dict], Awaitable[None]] | None = None

    @property
    def status(self) -> str:
        if self.changes:
            return "applied"
        return "skipped" if self.skipped else "unchanged"

    def summary(self) -> dict:
        return {
            "status": self.status,
            "mode": self.mode,
            "managed": self.managed,
            "changes": self.changes,
        }


def plan(group, mode, current: dict, version: int, managed: bool, values: dict, schema):
    """Plan an import of ``values`` over ``current`` (the editable fields of a policy row).

    Returns (target, changes, skipped). The file's values are validated in both modes.
    """
    if mode not in MODES:
        raise PolicyRejected(group, "mode must be initial or managed")
    proposed = validated(group, schema, merged(current, values)).model_dump(mode="json")
    pristine = version == 1
    target = proposed if mode == "managed" or pristine else current
    found = changes(current, target)
    if managed != (mode == "managed"):
        found["managed"] = [managed, mode == "managed"]
    skipped = mode == "initial" and not pristine and bool(changes(current, proposed))
    return target, found, skipped
