import base64
import binascii
import re
import socket
from ipaddress import IPv4Network, IPv6Network
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, HttpUrl, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url


class RuntimeSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="NEXUS_", frozen=True, extra="ignore", hide_input_in_errors=True
    )

    environment: Literal["development", "test", "production"] = "development"
    process_role: Literal["api", "worker"] = "api"
    database_url: SecretStr
    redis_url: SecretStr
    encryption_key: SecretStr
    encryption_key_version: int = Field(default=1, ge=1, le=2147483647)
    previous_encryption_keys: dict[int, SecretStr] = Field(default_factory=dict)
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=0, le=65535)
    db_pool_size: int = Field(default=5, ge=1, le=50)
    db_max_overflow: int = Field(default=0, ge=0, le=50)
    dependency_timeout: float = Field(default=3.0, gt=0, le=30)
    shutdown_timeout: int = Field(default=10, ge=1, le=60)
    worker_id: str = Field(default_factory=socket.gethostname)
    worker_interval: float = Field(default=5.0, ge=0.1, le=60)
    worker_heartbeat_ttl: int = Field(default=30, ge=2, le=300)
    # Source changes wake the Worker through PostgreSQL notifications; polling is a fallback.
    auto_publish_poll_seconds: float = Field(default=30.0, ge=1, le=3600)
    redis_namespace: str = "nexus:v1"
    log_level: Literal["debug", "info", "warning", "error"] = "info"
    mail_provider: Literal["smtp", "resend", "cloudflare"] | None = None
    mail_from: str | None = None
    smtp_host: str | None = None
    smtp_port: int = Field(default=587, ge=1, le=65535)
    smtp_security: Literal["starttls", "tls"] = "starttls"
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
    resend_api_key: SecretStr | None = None
    cloudflare_email_account_id: str | None = Field(default=None, pattern="^[a-fA-F0-9]{32}$")
    cloudflare_email_api_token: SecretStr | None = None
    turnstile_site_key: str | None = None
    turnstile_secret_key: SecretStr | None = None
    turnstile_hostname: str | None = None
    # TCP peers allowed to assert a single ingress-verified client address for auth limits: exact
    # addresses, or networks such as a Kubernetes pod CIDR where the BFF has no fixed address.
    # The BFF signature is required either way; the peer check only narrows who may present it.
    auth_proxy_peers: list[IPv4Network | IPv6Network] = Field(default_factory=list)
    # Shared with the BFF, which signs each asserted client address with it (HMAC-SHA256).
    auth_proxy_secret: SecretStr | None = None
    # Argon2 work runs in two separate thread pools so that sign-up and recovery cannot starve
    # sign-in. A request that waits longer than the queue timeout gets a retryable 503.
    login_hash_concurrency: int = Field(default=2, ge=1, le=64)
    account_hash_concurrency: int = Field(default=1, ge=1, le=64)
    hash_queue_timeout_seconds: float = Field(default=5.0, gt=0, le=60)
    payment_public_base_url: str | None = None
    payment_return_url: str | None = None
    mock_payment_url: str | None = None

    @field_validator("payment_public_base_url", "payment_return_url")
    @classmethod
    def payment_endpoint(cls, value):
        if value is None:
            return value
        try:
            parsed = HttpUrl(value)
            if (
                parsed.username is not None
                or parsed.password is not None
                or parsed.fragment is not None
                or parsed.query is not None
            ):
                raise ValueError
            return str(parsed).rstrip("/")
        except ValueError:
            raise ValueError(
                "payment endpoint must be a fixed HTTP(S) URL without credentials or query"
            ) from None

    @field_validator("mock_payment_url")
    @classmethod
    def mock_endpoint(cls, value):
        if value is None:
            return value
        try:
            parsed = urlsplit(value)
            if (
                parsed.scheme != "http"
                or parsed.hostname != "127.0.0.1"
                or not parsed.port
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
                or parsed.path not in {"", "/"}
            ):
                raise ValueError
        except ValueError:
            raise ValueError("mock payment provider must be a loopback HTTP endpoint") from None
        return value.rstrip("/")

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, value: SecretStr) -> SecretStr:
        try:
            url = make_url(value.get_secret_value())
            valid = (
                url.drivername == "postgresql+psycopg"
                and url.username in {"nexus_api", "nexus_worker"}
                and bool(url.password and url.host and url.database)
                and not ({"user", "password", "dbname", "service"} & url.query.keys())
            )
        except Exception:
            valid = False
        if not valid:
            raise ValueError("a PostgreSQL psycopg URL with a runtime role is required")
        return value

    @field_validator("redis_url")
    @classmethod
    def validate_redis_url(cls, value: SecretStr) -> SecretStr:
        try:
            url = urlsplit(value.get_secret_value())
            valid = url.scheme in {"redis", "rediss"} and bool(url.hostname and url.password)
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("an authenticated Redis URL is required")
        return value

    @field_validator("encryption_key")
    @classmethod
    def validate_key(cls, value: SecretStr) -> SecretStr:
        try:
            key = base64.b64decode(value.get_secret_value(), altchars=b"-_", validate=True)
        except ValueError, binascii.Error:
            raise ValueError("encryption key must encode 32 bytes") from None
        if len(key) != 32:
            raise ValueError("encryption key must encode 32 bytes")
        return value

    @field_validator("previous_encryption_keys")
    @classmethod
    def validate_previous_keys(cls, values):
        for version, secret in values.items():
            if not 1 <= version <= 2147483647:
                raise ValueError("invalid encryption key version")
            cls.validate_key(secret)
        return values

    @field_validator("auth_proxy_secret")
    @classmethod
    def validate_proxy_secret(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and len(value.get_secret_value()) < 32:
            raise ValueError("auth proxy secret must contain at least 32 characters")
        return value

    @field_validator("worker_id", "redis_namespace")
    @classmethod
    def validate_identifier(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9:_.-]{1,100}", value):
            raise ValueError("invalid runtime identifier")
        return value

    @model_validator(mode="after")
    def validate_runtime(self) -> RuntimeSettings:
        if self.encryption_key_version in self.previous_encryption_keys:
            raise ValueError("current encryption key version cannot also be historical")
        url = make_url(self.database_url.get_secret_value())
        if url.username != f"nexus_{self.process_role}":
            raise ValueError("database role must match the process role")
        if self.worker_heartbeat_ttl <= self.worker_interval + self.dependency_timeout:
            raise ValueError("heartbeat TTL must exceed the polling interval plus probe timeout")
        if self.auth_proxy_peers and self.auth_proxy_secret is None:
            # A peer address alone cannot tell the BFF from a co-located proxy that relays
            # client-supplied headers.
            raise ValueError("auth proxy peers require an auth proxy secret")
        if self.environment == "production":
            if self.mock_payment_url:
                raise ValueError("mock payment gateway is not permitted in production")
            if any(
                value and not value.startswith("https://")
                for value in (self.payment_public_base_url, self.payment_return_url)
            ):
                raise ValueError("production payment endpoints require HTTPS")
            if url.query.get("sslmode") != "verify-full":
                raise ValueError("production PostgreSQL requires sslmode=verify-full")
            if urlsplit(self.redis_url.get_secret_value()).scheme != "rediss":
                raise ValueError("production Redis requires TLS")
        return self


class MigrationSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="NEXUS_", frozen=True, extra="ignore", hide_input_in_errors=True
    )
    migration_database_url: SecretStr
    # Longest wait for a table lock before a migration statement fails; 0 waits indefinitely.
    migration_lock_timeout_ms: int = Field(default=5000, ge=0, le=3_600_000)
    # Longest run time of one migration statement; 0 (the default) does not limit it.
    migration_statement_timeout_ms: int = Field(default=0, ge=0, le=86_400_000)

    @field_validator("migration_database_url")
    @classmethod
    def validate_migrator(cls, value: SecretStr) -> SecretStr:
        try:
            url = make_url(value.get_secret_value())
            valid = (
                url.drivername == "postgresql+psycopg"
                and url.username == "nexus_migrator"
                and bool(url.password and url.host and url.database)
                and not ({"user", "password", "dbname", "service"} & url.query.keys())
            )
        except Exception:
            valid = False
        if not valid:
            raise ValueError("a separate PostgreSQL migration role is required")
        return value


class ProvisionSettings(BaseSettings):
    """Deployment-time administrator connection for ``nexus provision-database``.

    The URL names a role that may create roles and databases (a superuser or one with CREATEROLE
    and CREATEDB) and the maintenance database to connect to, normally ``postgres``. Login role
    passwords are optional: roles that already exist, for example CloudNativePG managed roles, keep
    theirs unless a new one is given.
    """

    model_config = SettingsConfigDict(
        env_prefix="NEXUS_PROVISION_", frozen=True, extra="ignore", hide_input_in_errors=True
    )
    database_url: SecretStr
    database: str = Field(default="nexus", pattern=r"^[a-z_][a-z0-9_]{0,62}$")
    api_password: SecretStr | None = None
    worker_password: SecretStr | None = None
    migrator_password: SecretStr | None = None

    @field_validator("database_url")
    @classmethod
    def validate_admin_url(cls, value: SecretStr) -> SecretStr:
        try:
            url = make_url(value.get_secret_value())
            valid = url.drivername in {"postgresql", "postgres", "postgresql+psycopg"} and bool(
                url.username and url.host and url.database
            )
        except Exception:
            valid = False
        if not valid:
            raise ValueError("a PostgreSQL administrator URL with user, host and database")
        return value

    def passwords(self) -> dict[str, str]:
        values = {
            "nexus_api": self.api_password,
            "nexus_worker": self.worker_password,
            "nexus_migrator": self.migrator_password,
        }
        return {role: secret.get_secret_value() for role, secret in values.items() if secret}
