import os
from types import MappingProxyType
from uuid import UUID

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from nexus.shared.errors import ServiceError


class CredentialCipher:
    """Versioned AEAD storage with separate key derivation and record-bound AAD."""

    def __init__(self, current_version: int, roots: dict[int, bytes], *, purpose="catalog"):
        if current_version not in roots or any(len(key) != 32 for key in roots.values()):
            raise ValueError("invalid credential encryption keyring")
        self.current_version = current_version
        if purpose not in {"catalog", "payment", "identity-mail"}:
            raise ValueError("unsupported credential purpose")
        self._purpose = purpose
        self._keys = MappingProxyType(
            {
                version: HKDF(
                    algorithm=hashes.SHA256(),
                    length=32,
                    salt=None,
                    info=f"nexus:{purpose}-credentials:v1".encode(),
                ).derive(root)
                for version, root in roots.items()
            }
        )

    def supports(self, version: int) -> bool:
        return version in self._keys

    @property
    def versions(self) -> frozenset[int]:
        return frozenset(self._keys)

    def _aad(self, upstream_id: UUID, credential_id: UUID, version: int) -> bytes:
        kind = {
            "catalog": "upstream-credential",
            "payment": "payment-configuration",
            "identity-mail": "identity-mail",
        }[self._purpose]
        return f"nexus:{kind}:v1:{upstream_id}:{credential_id}:{version}".encode()

    def encrypt(self, secret: str, upstream_id: UUID, credential_id: UUID) -> tuple[int, bytes]:
        version = self.current_version
        nonce = os.urandom(12)
        ciphertext = AESGCM(self._keys[version]).encrypt(
            nonce, secret.encode(), self._aad(upstream_id, credential_id, version)
        )
        return version, nonce + ciphertext

    def decrypt(
        self, ciphertext: bytes, upstream_id: UUID, credential_id: UUID, version: int
    ) -> str:
        try:
            if len(ciphertext) < 28:
                raise InvalidTag
            return (
                AESGCM(self._keys[version])
                .decrypt(
                    ciphertext[:12], ciphertext[12:], self._aad(upstream_id, credential_id, version)
                )
                .decode()
            )
        except KeyError, InvalidTag, ValueError, UnicodeError:
            raise ServiceError(
                "credential_unavailable", "Credential cannot be decrypted", 503
            ) from None
