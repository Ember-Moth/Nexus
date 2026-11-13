import base64
from uuid import uuid4

import pytest
from pydantic import SecretStr, ValidationError

from nexus.infrastructure.credential_cipher import CredentialCipher
from nexus.shared.errors import ServiceError


def test_cipher_authenticates_record_context_and_uses_fresh_nonces():
    cipher = CredentialCipher(1, {1: bytes(range(32))})
    upstream, credential = uuid4(), uuid4()
    secret = "provider-secret-for-test"
    version, encrypted = cipher.encrypt(secret, upstream, credential)
    assert encrypted != cipher.encrypt(secret, upstream, credential)[1]
    assert secret.encode() not in encrypted
    assert cipher.decrypt(encrypted, upstream, credential, version) == secret
    for payload, parent, identity, key_version in [
        (encrypted[:-1] + bytes([encrypted[-1] ^ 1]), upstream, credential, 1),
        (encrypted, uuid4(), credential, 1),
        (encrypted, upstream, uuid4(), 1),
        (encrypted, upstream, credential, 2),
        (b"bad", upstream, credential, 1),
    ]:
        with pytest.raises(ServiceError) as error:
            cipher.decrypt(payload, parent, identity, key_version)
        assert error.value.code == "credential_unavailable"
        assert secret not in str(error.value)


def test_keyring_preserves_old_ciphertexts_during_rotation():
    parent, identity = uuid4(), uuid4()
    first = CredentialCipher(1, {1: b"a" * 32})
    _, original = first.encrypt("old-value", parent, identity)
    rotated = CredentialCipher(2, {1: b"a" * 32, 2: b"b" * 32})
    assert rotated.decrypt(original, parent, identity, 1) == "old-value"
    version, new = rotated.encrypt("new-value", parent, identity)
    assert version == 2 and rotated.decrypt(new, parent, identity, 2) == "new-value"
    with pytest.raises(ServiceError):
        CredentialCipher(2, {2: b"b" * 32}).decrypt(original, parent, identity, 1)


def test_keyring_settings_are_validated_and_redacted(settings_factory):
    encoded = base64.urlsafe_b64encode(b"a" * 32).decode()
    settings = settings_factory(
        encryption_key_version=2, previous_encryption_keys={1: SecretStr(encoded)}
    )
    assert encoded not in settings.model_dump_json()
    assert encoded not in repr(settings)
    with pytest.raises(ValidationError):
        settings_factory(previous_encryption_keys={1: SecretStr(encoded)})
    with pytest.raises(ValidationError):
        settings_factory(previous_encryption_keys={2: SecretStr("invalid")})


def test_configuration_cli_never_echoes_secret_dictionary_keys(
    monkeypatch, capsys, settings_factory
):
    import json
    import sys

    from nexus.cli import main

    settings = settings_factory()
    marker = "secret-material-pasted-as-a-dictionary-key"
    monkeypatch.setenv("NEXUS_DATABASE_URL", settings.database_url.get_secret_value())
    monkeypatch.setenv("NEXUS_REDIS_URL", settings.redis_url.get_secret_value())
    monkeypatch.setenv("NEXUS_ENCRYPTION_KEY", settings.encryption_key.get_secret_value())
    monkeypatch.setenv("NEXUS_PREVIOUS_ENCRYPTION_KEYS", json.dumps({marker: "invalid"}))
    monkeypatch.setattr(sys, "argv", ["nexus", "check"])
    assert main() == 1
    output = capsys.readouterr()
    assert "previous_encryption_keys" in output.err
    assert marker not in output.err and marker not in output.out
