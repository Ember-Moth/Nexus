import argparse
import getpass
import json
import logging
import sys

import uvicorn
import uvloop
from pydantic import ValidationError

from nexus.api import create_app
from nexus.commands import bootstrap, check, migrate, worker_health
from nexus.config import MigrationSettings, RuntimeSettings
from nexus.shared.errors import BootstrapRejected
from nexus.worker import serve_worker


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="nexus")
    result.add_argument("--env-file", help="process-specific environment profile")
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("api", help="run the control-plane HTTP server")
    commands.add_parser("worker", help="run worker lifecycle and dependency supervision")
    commands.add_parser("worker-health", help="check this worker identity's live heartbeat")
    commands.add_parser("check", help="check API role, schema revision and dependencies")
    migrations = commands.add_parser(
        "migrate", help="upgrade schema using separate migration credentials"
    )
    migrations.add_argument("--revision", default="head")
    administrator = commands.add_parser(
        "bootstrap-admin", help="create the first platform administrator"
    )
    administrator.add_argument("--login", required=True)
    administrator.add_argument("--password-stdin", action="store_true")
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        if args.command == "migrate":
            migrate(MigrationSettings(_env_file=args.env_file), revision=args.revision)
            print("Database migration completed.")
            return 0
        role = "worker" if args.command in {"worker", "worker-health"} else "api"
        settings = RuntimeSettings(_env_file=args.env_file, process_role=role)
        logging.basicConfig(
            level=settings.log_level.upper(), format="%(levelname)s %(name)s %(message)s"
        )
        if args.command == "api":
            uvicorn.run(
                create_app(settings),
                host=settings.host,
                port=settings.port,
                loop="uvloop",
                log_level=settings.log_level,
                timeout_graceful_shutdown=settings.shutdown_timeout,
                access_log=False,
                proxy_headers=False,
            )
        elif args.command == "worker":
            uvloop.run(serve_worker(settings))
        elif args.command == "worker-health":
            healthy = uvloop.run(worker_health(settings))
            print(json.dumps({"ready": healthy}))
            return 0 if healthy else 1
        elif args.command == "check":
            checks = uvloop.run(check(settings))
            print(json.dumps(checks))
            return 0 if all(value == "ok" for value in checks.values()) else 1
        elif args.command == "bootstrap-admin":
            if args.password_stdin:
                password = sys.stdin.readline(2049).rstrip("\r\n")
            else:
                password = getpass.getpass("Administrator password: ")
                if password != getpass.getpass("Confirm password: "):
                    raise BootstrapRejected("passwords do not match")
            print(json.dumps(uvloop.run(bootstrap(settings, args.login, password))))
        return 0
    except ValidationError as exc:
        fields = ", ".join(
            ".".join(str(part) for part in item["loc"]) or "settings" for item in exc.errors()
        )
        print(
            f"Invalid configuration fields: {fields}. Check the selected profile.", file=sys.stderr
        )
    except BootstrapRejected as exc:
        print(str(exc), file=sys.stderr)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        # Never print connection strings, credentials or SQL parameter values.
        print(
            f"Command failed ({type(exc).__name__}). Check configuration and dependency readiness.",
            file=sys.stderr,
        )
    return 1


if __name__ == "__main__":
    sys.exit(main())
