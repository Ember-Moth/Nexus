from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, slots=True)
class BootstrapResult:
    user_id: UUID
    created: bool
