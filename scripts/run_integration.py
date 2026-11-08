import os
import subprocess
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

from native_services import NativeServices


def main() -> int:
    root = Path(__file__).resolve().parents[1]
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
            )
            if migration.returncode:
                return migration.returncode
            environment = dict(os.environ, NEXUS_TEST_SERVICES=str(workspace))
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
