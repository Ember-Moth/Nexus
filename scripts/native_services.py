"""Repository-local PostgreSQL/Redis; never controls system service managers."""

import base64
import json
import os
import secrets
import shlex
import shutil
import socket
import subprocess
import time
from pathlib import Path
from urllib.parse import quote

import psycopg
from psycopg import sql
from redis import Redis
from redis.exceptions import RedisError

MODULES = ("identity", "catalog", "billing", "publication", "reporting")


def executable(name: str) -> str:
    found = shutil.which(name)
    if found:
        return found
    pg_config = shutil.which("pg_config")
    if pg_config and name in {"postgres", "initdb", "pg_ctl"}:
        bindir = subprocess.check_output([pg_config, "--bindir"], text=True).strip()
        candidate = Path(bindir) / name
        if candidate.is_file():
            return str(candidate)
    raise RuntimeError(f"Required native executable is missing: {name}")


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def write_private(path: Path, value: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(value)


class NativeServices:
    def __init__(self, root: Path, *, database: str = "nexus") -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.metadata = self.root / "services.json"
        if not self.metadata.exists():
            data = {
                "database": database,
                "pg_port": free_port(),
                "redis_port": free_port(),
                "admin_password": secrets.token_urlsafe(32),
                "api_password": secrets.token_urlsafe(32),
                "worker_password": secrets.token_urlsafe(32),
                "migrator_password": secrets.token_urlsafe(32),
                "redis_password": secrets.token_urlsafe(32),
                "encryption_key": base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),
            }
            while data["redis_port"] == data["pg_port"]:
                data["redis_port"] = free_port()
            write_private(self.metadata, json.dumps(data))
        self.data = json.loads(self.metadata.read_text())
        if self.data["database"] != database:
            raise RuntimeError("native service directory belongs to another database")
        self.pg_data = self.root / "postgres"
        self.socket_dir = self.root / "socket"
        self.socket_dir.mkdir(exist_ok=True, mode=0o700)

    def _run(self, args: list[str], *, timeout: int = 30, check: bool = True):
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        if check and result.returncode:
            diagnostic = (result.stdout + result.stderr)[-2000:]
            for key, value in self.data.items():
                if "password" in key or key == "encryption_key":
                    diagnostic = diagnostic.replace(str(value), "[hidden]")
            raise RuntimeError(f"{Path(args[0]).name} failed: {diagnostic}")
        return result

    def admin_connection(self, database: str | None = None):
        return psycopg.connect(
            host="127.0.0.1",
            port=self.data["pg_port"],
            user="nexus_local_admin",
            password=self.data["admin_password"],
            dbname=database or self.data["database"],
            autocommit=True,
            connect_timeout=3,
        )

    def redis_client(self) -> Redis:
        return Redis(
            host="127.0.0.1",
            port=self.data["redis_port"],
            password=self.data["redis_password"],
            socket_connect_timeout=1,
            socket_timeout=1,
            decode_responses=True,
        )

    def start(self) -> None:
        for name in ("initdb", "pg_ctl", "redis-server"):
            executable(name)
        if not (self.pg_data / "PG_VERSION").exists():
            password_file = self.root / "initdb.password"
            write_private(password_file, self.data["admin_password"] + "\n")
            try:
                self._run(
                    [
                        executable("initdb"),
                        "-D",
                        str(self.pg_data),
                        "--username=nexus_local_admin",
                        "--auth-local=trust",
                        "--auth-host=scram-sha-256",
                        "--encoding=UTF8",
                        "--locale=C",
                        f"--pwfile={password_file}",
                    ]
                )
            finally:
                password_file.unlink(missing_ok=True)
        status = self._run([executable("pg_ctl"), "-D", str(self.pg_data), "status"], check=False)
        if status.returncode:
            options = shlex.join(
                [
                    "-h",
                    "127.0.0.1",
                    "-p",
                    str(self.data["pg_port"]),
                    "-k",
                    str(self.socket_dir),
                ]
            )
            self._run(
                [
                    executable("pg_ctl"),
                    "-D",
                    str(self.pg_data),
                    "-l",
                    str(self.root / "postgres.log"),
                    "-o",
                    options,
                    "-w",
                    "-t",
                    "15",
                    "start",
                ]
            )
        self._provision_roles()
        redis_config = self.root / "redis.conf"
        if not redis_config.exists():
            write_private(
                redis_config,
                "\n".join(
                    [
                        "bind 127.0.0.1",
                        "protected-mode yes",
                        f"port {self.data['redis_port']}",
                        f"requirepass {self.data['redis_password']}",
                        "daemonize yes",
                        'save ""',
                        "appendonly no",
                        f'pidfile "{self.root / "redis.pid"}"',
                        f'logfile "{self.root / "redis.log"}"',
                        f'dir "{self.root}"',
                        "",
                    ]
                ),
            )
        with self.redis_client() as client:
            try:
                running = client.ping()
            except Exception:
                running = False
        if not running:
            self._run([executable("redis-server"), str(redis_config)])
            deadline = time.monotonic() + 10
            while True:
                try:
                    with self.redis_client() as client:
                        if client.ping():
                            break
                except Exception:
                    if time.monotonic() >= deadline:
                        raise RuntimeError("isolated Redis did not become ready") from None
                time.sleep(0.1)

    def _provision_roles(self) -> None:
        with self.admin_connection("postgres") as connection:
            if connection.info.server_version < 160000:
                raise RuntimeError("PostgreSQL 16 or newer is required")
            for module in (*MODULES, "bridge", "gateway"):
                role = f"nexus_{module}"
                if not connection.execute(
                    "SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)
                ).fetchone():
                    connection.execute(
                        sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(role))
                    )
            for kind in ("api", "worker", "migrator"):
                role = f"nexus_{kind}"
                exists = connection.execute(
                    "SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)
                ).fetchone()
                if not exists:
                    connection.execute(
                        sql.SQL(
                            "CREATE ROLE {} LOGIN NOINHERIT NOSUPERUSER "
                            "NOCREATEDB NOCREATEROLE PASSWORD {}"
                        ).format(sql.Identifier(role), sql.Literal(self.data[f"{kind}_password"]))
                    )
            db_name = self.data["database"]
            if not connection.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s", (db_name,)
            ).fetchone():
                connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(db_name)))
            connection.execute(
                sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(sql.Identifier(db_name))
            )
            connection.execute(
                sql.SQL(
                    "GRANT CONNECT ON DATABASE {} TO nexus_api, nexus_worker, nexus_migrator"
                ).format(sql.Identifier(db_name))
            )
            connection.execute(
                sql.SQL("GRANT CREATE ON DATABASE {} TO nexus_migrator").format(
                    sql.Identifier(db_name)
                )
            )
            for module in MODULES:
                connection.execute(
                    sql.SQL("GRANT {} TO nexus_api, nexus_worker").format(
                        sql.Identifier(f"nexus_{module}")
                    )
                )
            for owner in (*MODULES, "bridge"):
                connection.execute(
                    sql.SQL("GRANT {} TO nexus_migrator").format(sql.Identifier(f"nexus_{owner}"))
                )
            connection.execute(
                sql.SQL("GRANT CONNECT ON DATABASE {} TO nexus_gateway").format(
                    sql.Identifier(db_name)
                )
            )
        with self.admin_connection() as connection:
            connection.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")

    def profiles(self, destination: Path) -> None:
        destination.mkdir(parents=True, exist_ok=True)
        common = {
            "NEXUS_ENVIRONMENT": "development",
            "NEXUS_REDIS_URL": f"redis://:{quote(self.data['redis_password'])}@127.0.0.1:{self.data['redis_port']}/0",
            "NEXUS_ENCRYPTION_KEY": self.data["encryption_key"],
            "NEXUS_WORKER_ID": "nexus-local",
        }
        for kind in ("api", "worker", "migrator"):
            data = {} if kind == "migrator" else dict(common)
            key = "NEXUS_MIGRATION_DATABASE_URL" if kind == "migrator" else "NEXUS_DATABASE_URL"
            data[key] = (
                f"postgresql+psycopg://nexus_{kind}:{quote(self.data[f'{kind}_password'])}"
                f"@127.0.0.1:{self.data['pg_port']}/{self.data['database']}"
            )
            name = "migrate" if kind == "migrator" else kind
            target = destination / f".env.{name}"
            if not target.exists():
                write_private(target, "".join(f"{key}={value}\n" for key, value in data.items()))

    def stop(self) -> None:
        # Verify the local pid before asking Redis to exit.
        pid_file = self.root / "redis.pid"
        if pid_file.exists():
            with self.redis_client() as client:
                try:
                    if client.info("server")["process_id"] == int(pid_file.read_text().strip()):
                        client.shutdown(nosave=True)
                except RedisError, OSError:
                    pass
        if (self.pg_data / "PG_VERSION").exists():
            status = self._run(
                [executable("pg_ctl"), "-D", str(self.pg_data), "status"], check=False
            )
            if status.returncode == 0:
                self._run(
                    [
                        executable("pg_ctl"),
                        "-D",
                        str(self.pg_data),
                        "-m",
                        "fast",
                        "-w",
                        "-t",
                        "15",
                        "stop",
                    ]
                )
