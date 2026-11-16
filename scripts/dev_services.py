import argparse
from pathlib import Path

from native_services import NativeServices


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage repository-local native services")
    parser.add_argument("action", choices=("start", "stop"))
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    services = NativeServices(root / ".local" / "native")
    if args.action == "start":
        services.start()
        services.profiles(root)
        print("PostgreSQL, Redis and S3 (moto) ready. Profiles: .env.api .env.worker .env.migrate")
        print("Run: uv run nexus --env-file .env.migrate migrate")
    else:
        services.stop()
        print("Repository-local services stopped; data retained.")


if __name__ == "__main__":
    main()
