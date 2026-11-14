import base64
from uuid import UUID

from nacl.exceptions import CryptoError
from nacl.public import PublicKey, SealedBox
from pydantic import BaseModel, ConfigDict, Field, field_validator

CAPABILITIES = ("reservation.v1", "usage-events.v1")


class Command(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Cluster(Command):
    name: str = Field(min_length=1, max_length=160)
    region: str = Field(min_length=1, max_length=100)
    organization_ids: list[UUID] = Field(min_length=1, max_length=256)
    deployment_ids: list[UUID] = Field(min_length=1, max_length=1024)
    public_key: str

    @field_validator("public_key")
    @classmethod
    def valid_key(cls, value):
        try:
            key = PublicKey(base64.b64decode(value, validate=True))
            SealedBox(key).encrypt(b"")
        except ValueError, CryptoError:
            raise ValueError("Expected a valid base64 Curve25519 public key") from None
        return value


class Principal(Command):
    database_role: str = Field(pattern=r"^[a-z_][a-z0-9_]{0,62}$")
    node_id: UUID
    capabilities: list[str] = Field(default_factory=lambda: list(CAPABILITIES), max_length=32)
    active: bool = True


class Release(Command):
    rollback_revision: int | None = Field(default=None, gt=0)


class ClusterUpdate(Command):
    active: bool | None = None
    public_key: str | None = None
    key_grace_seconds: int = Field(default=3600, ge=0, le=86400)
    organization_ids: list[UUID] | None = Field(default=None, min_length=1, max_length=256)
    deployment_ids: list[UUID] | None = Field(default=None, min_length=1, max_length=1024)

    @field_validator("public_key")
    @classmethod
    def valid_key(cls, value):
        return Cluster.valid_key(value) if value is not None else None
