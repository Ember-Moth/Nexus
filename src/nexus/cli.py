import argparse
import getpass
import json
import sys

import uvicorn
import uvloop
from pydantic import ValidationError

from nexus.api import create_app
from nexus.commands import (
    bootstrap,
    check,
    import_policies,
    migrate,
    provision_database,
    worker_health,
)
from nexus.config import MigrationSettings, ProvisionSettings, RuntimeSettings
from nexus.infrastructure.provisioning import ProvisioningError
from nexus.shared.errors import BootstrapRejected, ServiceError
from nexus.shared.policy import PolicyRejected
from nexus.shared.telemetry import configure_logging
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
    commands.add_parser(
        "provision-database",
        help="create the database, group roles and grants with an administrator connection",
    )
    administrator = commands.add_parser(
        "bootstrap-admin", help="create the first platform administrator"
    )
    administrator.add_argument("--login", required=True)
    administrator.add_argument("--password-stdin", action="store_true")
    policies = commands.add_parser("settings", help="business policies stored in the database")
    policy_commands = policies.add_subparsers(dest="settings_command", required=True)
    importer = policy_commands.add_parser(
        "import", help="apply a YAML or JSON policy file (initial values or managed groups)"
    )
    importer.add_argument("--file", required=True, help="policy file (.yaml, .yml or .json)")
    importer.add_argument(
        "--dry-run", action="store_true", help="report the changes without writing them"
    )
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        if args.command == "migrate":
            migrate(MigrationSettings(_env_file=args.env_file), revision=args.revision)
            print("Database migration completed.")
            return 0
        if args.command == "provision-database":
            provision_database(ProvisionSettings(_env_file=args.env_file))
            print("Database roles and grants are in place.")
            return 0
        role = "worker" if args.command in {"worker", "worker-health"} else "api"
        settings = RuntimeSettings(_env_file=args.env_file, process_role=role)
        configure_logging(settings.log_level)
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
        elif args.command == "settings":
            result = uvloop.run(import_policies(settings, args.file, dry_run=args.dry_run))
            print(json.dumps(result, default=str))
        return 0
    except ValidationError as exc:
        # Nested dictionary keys can themselves contain mistakenly pasted secret material.
        fields = ", ".join(
            sorted({str(item["loc"][0]) if item["loc"] else "settings" for item in exc.errors()})
        )
        print(
            f"Invalid configuration fields: {fields}. Check the selected profile.", file=sys.stderr
        )
    except (BootstrapRejected, ProvisioningError) as exc:
        print(str(exc), file=sys.stderr)
    except PolicyRejected as exc:
        print(f"Policy file rejected, nothing was written: {exc}", file=sys.stderr)
    except ServiceError as exc:
        print(f"Policy import stopped ({exc.code}): {exc}", file=sys.stderr)
    except OSError as exc:
        print(f"Cannot read the policy file ({type(exc).__name__}).", file=sys.stderr)
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
