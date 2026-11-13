import os
import subprocess
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

from native_services import NativeServices


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    # Never let a developer's exported application credentials override isolated profiles.
    isolated_environment = {
        key: value for key, value in os.environ.items() if not key.startswith("NEXUS_")
    }
    with tempfile.TemporaryDirectory(prefix="nexus-test-") as directory:
        workspace = Path(directory)
        services = NativeServices(workspace, database=f"nexus_test_{uuid4().hex[:12]}")
        try:
            services.start()
            services.profiles(workspace)
            migration = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "nexus.cli",
                    "--env-file",
                    str(workspace / ".env.migrate"),
                    "migrate",
                ],
                cwd=root,
                env=isolated_environment,
            )
            if migration.returncode:
                return migration.returncode
            environment = dict(isolated_environment, NEXUS_TEST_SERVICES=str(workspace))
            return subprocess.call(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "-m",
                    "integration",
                    *sys.argv[1:],
                ],
                env=environment,
                cwd=root,
            )
        finally:
            services.stop()


if __name__ == "__main__":
    sys.exit(main())
