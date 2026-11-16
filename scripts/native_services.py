"""Repository-local PostgreSQL, Redis and S3 (moto); never controls system service managers."""

import base64
import json
import os
import secrets
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote

import boto3
import psycopg
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from redis import Redis
from redis.exceptions import RedisError

from nexus.infrastructure.provisioning import MODULES, provision

__all__ = ["BUCKET", "MODULES", "NativeServices", "executable"]

# The application bucket; production uses nexus-app on Cloudflare R2.
BUCKET = "nexus-app"
# moto accepts any region but creates buckets without a location constraint only in us-east-1.
REGION = "us-east-1"


def executable(name: str) -> str:
    found = shutil.which(name)
    if found:
        return found
    pg_config = shutil.which("pg_config")
    if pg_config and name in {"postgres", "initdb", "pg_ctl", "pg_basebackup", "pg_verifybackup"}:
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


def replace_private(path: Path, value: str) -> None:
    staged = path.with_name(path.name + ".new")
    staged.unlink(missing_ok=True)
    write_private(staged, value)
    staged.replace(path)


def _secrets() -> dict:
    return {
        "admin_password": secrets.token_urlsafe(32),
        "api_password": secrets.token_urlsafe(32),
        "worker_password": secrets.token_urlsafe(32),
        "migrator_password": secrets.token_urlsafe(32),
        "redis_password": secrets.token_urlsafe(32),
        "encryption_key": base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),
        "s3_access_key": "nexus" + secrets.token_hex(8),
        "s3_secret_key": secrets.token_urlsafe(30),
    }


class NativeServices:
    def __init__(
        self,
        root: Path,
        *,
        database: str = "nexus",
        track_commits: bool = False,
        options: tuple[str, ...] = (),
    ) -> None:
        self.track_commits = track_commits
        # Extra `-c name=value` server settings, e.g. for load runs with many nodes.
        self.options = options
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.metadata = self.root / "services.json"
        if not self.metadata.exists():
            data = {"database": database, "pg_port": free_port(), **_secrets()}
            write_private(self.metadata, json.dumps(data))
        self.data = json.loads(self.metadata.read_text())
        if self.data["database"] != database:
            raise RuntimeError("native service directory belongs to another database")
        # Directories created before a service existed gain its port and credentials.
        missing = {k: v for k, v in _secrets().items() if k not in self.data}
        for name in ("redis_port", "s3_port"):
            if name not in self.data:
                taken = {self.data.get(k) for k in ("pg_port", "redis_port", "s3_port")}
                port = free_port()
                while port in taken:
                    port = free_port()
                missing[name] = self.data[name] = port
        if missing:
            self.data.update(missing)
            replace_private(self.metadata, json.dumps(self.data))
        self.pg_data = self.root / "postgres"
        self.socket_dir = self.root / "socket"
        self.socket_dir.mkdir(exist_ok=True, mode=0o700)

    def _run(self, args: list[str], *, timeout: int = 30, check: bool = True):
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        if check and result.returncode:
            diagnostic = (result.stdout + result.stderr)[-2000:]
            for key, value in self.data.items():
                if "password" in key or key in {"encryption_key", "s3_secret_key"}:
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

    def object_storage(self):
        """An S3 client of the local store, signed with the application's credentials."""
        return boto3.session.Session().client(
            "s3",
            endpoint_url=f"http://127.0.0.1:{self.data['s3_port']}",
            region_name=REGION,
            aws_access_key_id=self.data["s3_access_key"],
            aws_secret_access_key=self.data["s3_secret_key"],
            config=Config(
                s3={"addressing_style": "path"},
                connect_timeout=1,
                read_timeout=5,
                retries={"max_attempts": 1},
            ),
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
                    *(["-c", "track_commit_timestamp=on"] if self.track_commits else []),
                    *(arg for option in self.options for arg in ("-c", option)),
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
        self.start_object_storage()

    def _s3_pid(self) -> int | None:
        """The pid of this directory's moto server, if it is still that process."""
        pid_file = self.root / "s3.pid"
        if not pid_file.exists():
            return None
        pid = int(pid_file.read_text().strip())
        command = self._run(["ps", "-o", "command=", "-p", str(pid)], check=False).stdout
        if "moto.server" in command and str(self.data["s3_port"]) in command:
            return pid
        pid_file.unlink(missing_ok=True)
        return None

    def start_object_storage(self) -> None:
        """An S3-compatible moto server on loopback with the application bucket.

        moto keeps objects in memory: a restart loses the export files, not the export jobs.
        """
        if self._s3_pid() is None:
            with open(self.root / "s3.log", "ab") as log:
                process = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "moto.server",
                        "-H",
                        "127.0.0.1",
                        "-p",
                        str(self.data["s3_port"]),
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    cwd=self.root,
                    start_new_session=True,
                )
            (self.root / "s3.pid").write_text(str(process.pid))
        client = self.object_storage()
        deadline = time.monotonic() + 30
        while True:
            try:
                client.head_bucket(Bucket=BUCKET)
                return
            except ClientError:
                client.create_bucket(Bucket=BUCKET)
                return
            except BotoCoreError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("isolated S3 service did not become ready") from None
            time.sleep(0.1)

    def _provision_roles(self) -> None:
        provision(
            self.admin_connection,
            self.data["database"],
            {
                f"nexus_{kind}": self.data[f"{kind}_password"]
                for kind in ("api", "worker", "migrator")
            },
        )

    def profiles(self, destination: Path) -> None:
        destination.mkdir(parents=True, exist_ok=True)
        common = {
            "NEXUS_ENVIRONMENT": "development",
            "NEXUS_REDIS_URL": f"redis://:{quote(self.data['redis_password'])}@127.0.0.1:{self.data['redis_port']}/0",
            "NEXUS_ENCRYPTION_KEY": self.data["encryption_key"],
            "NEXUS_WORKER_ID": "nexus-local",
            "NEXUS_OBJECT_STORAGE_ENDPOINT_URL": f"http://127.0.0.1:{self.data['s3_port']}",
            "NEXUS_OBJECT_STORAGE_BUCKET": BUCKET,
            "NEXUS_OBJECT_STORAGE_REGION": REGION,
            "NEXUS_OBJECT_STORAGE_ACCESS_KEY_ID": self.data["s3_access_key"],
            "NEXUS_OBJECT_STORAGE_SECRET_ACCESS_KEY": self.data["s3_secret_key"],
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
                continue
            # An existing profile keeps its values and gains the settings it lacks.
            text = target.read_text()
            present = {line.split("=", 1)[0] for line in text.splitlines() if "=" in line}
            extra = "".join(f"{k}={v}\n" for k, v in data.items() if k not in present)
            if extra:
                replace_private(target, text + ("" if text.endswith("\n") else "\n") + extra)

    def stop(self) -> None:
        self.stop_object_storage()
        # Verify the local pid before asking Redis to exit.
        pid_file = self.root / "redis.pid"
        if pid_file.exists():
            with self.redis_client() as client:
                try:
                    if client.info("server")["process_id"] == int(pid_file.read_text().strip()):
                        client.shutdown(nosave=True)
                except RedisError, OSError:
                    pass
        self.stop_postgres()

    def stop_postgres(self) -> None:
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

    def stop_object_storage(self) -> None:
        pid = self._s3_pid()
        if pid is not None:
            try:
                os.kill(pid, signal.SIGTERM)
                deadline = time.monotonic() + 10
                while self._s3_pid() is not None and time.monotonic() < deadline:
                    time.sleep(0.05)
            except ProcessLookupError:
                pass
        (self.root / "s3.pid").unlink(missing_ok=True)
