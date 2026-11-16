"""Record what the migrations build on an empty database, and compare two such records.

Each capture starts a throwaway PostgreSQL cluster (``NativeServices.start_postgres``: no Redis
or object storage), provisions it the way deployments do, runs ``alembic upgrade head`` with the
migrations of the working tree or of a git revision, and writes:

- ``schema.sql``: ``pg_dump --schema-only`` of the database, with owners and privileges:
  schemas, tables, constraints, indexes, sequences, views, functions, triggers, row-level
  security, GRANT/REVOKE and default privileges;
- ``roles.sql``: ``pg_dumpall --roles-only --no-role-passwords``, the cluster's roles, their
  attributes, settings and memberships;
- ``catalog.txt``: default privileges and the database's ACL and role settings as stored;
- ``data.txt``: every row the migrations wrote, table by table, sorted, and sequence states.
  ``nexus_meta.alembic_version`` is left out.

Two migration runs never agree on wall-clock time or on generated UUIDs, so ``data.txt`` shows a
timestamp taken while the migrations ran as ``<migrated at>`` and a random UUID (any UUID that is
not a fixed ``00000000-…`` seed) as ``<uuid N>``, numbered in order of first appearance. Equal
numbers mean equal values, so references between rows are still compared.

Usage (set TMPDIR to a short directory on macOS; socket paths are limited to 103 bytes)::

    uv run python scripts/schema_evidence.py check [--ref REV]   # REV against the working tree
    uv run python scripts/schema_evidence.py capture OUT [--ref REV]
    uv run python scripts/schema_evidence.py compare A B

``check`` without ``--ref`` compares against the last revision before
``migrations/versions/0001_baseline.py`` was added, the one whose migration chain the baseline
replaced. It exits with status 1 when the records differ.
"""

import argparse
import difflib
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent))
from native_services import NativeServices, executable  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DATABASE = "nexus"
SCHEMAS = ("identity", "catalog", "billing", "publication", "reporting", "gateway_v1", "nexus_meta")
SKIPPED = {"nexus_meta.alembic_version"}
FILES = ("schema.sql", "roles.sql", "catalog.txt", "data.txt")
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
# pg_dump 17.6+ fences its output with a random key; it says nothing about the database.
VOLATILE = re.compile(r"^(\\restrict |\\unrestrict |-- Dumped (from|by) )")


def baseline_parent() -> str:
    added = subprocess.check_output(
        [
            "git",
            "log",
            "--diff-filter=A",
            "--format=%H",
            "--",
            "migrations/versions/0001_baseline.py",
        ],
        cwd=ROOT,
        text=True,
    ).split()
    if not added:
        raise SystemExit("no commit adds migrations/versions/0001_baseline.py; pass --ref")
    return added[-1] + "^"


def source_tree(ref: str | None, workspace: Path) -> Path:
    """The tree whose migrations run: the working tree, or REV extracted into the workspace."""
    if ref is None:
        return ROOT
    target = workspace / "source"
    target.mkdir()
    archive = subprocess.run(
        ["git", "archive", "--format=tar", ref, "alembic.ini", "migrations", "src/nexus"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout
    subprocess.run(["tar", "-x", "-C", str(target)], input=archive, check=True)
    return target


def migrate(services: NativeServices, source: Path) -> None:
    url = (
        f"postgresql+psycopg://nexus_migrator:{quote(services.data['migrator_password'])}"
        f"@127.0.0.1:{services.data['pg_port']}/{DATABASE}"
    )
    environment = {k: v for k, v in os.environ.items() if not k.startswith("NEXUS_")}
    environment["NEXUS_MIGRATION_DATABASE_URL"] = url
    # The extracted revision's own nexus package comes first, ahead of the installed one.
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(source / "src"), *filter(None, [os.environ.get("PYTHONPATH")])]
    )
    subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(source / "alembic.ini"), "upgrade", "head"],
        cwd=source,
        env=environment,
        check=True,
    )


def dump(services: NativeServices, command: str, *args: str) -> str:
    environment = dict(os.environ, PGPASSWORD=services.data["admin_password"])
    output = subprocess.run(
        [
            executable(command),
            "--host=127.0.0.1",
            f"--port={services.data['pg_port']}",
            "--username=nexus_local_admin",
            *args,
        ],
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return "".join(line for line in output.splitlines(True) if not VOLATILE.match(line))


def catalog(connection: psycopg.Connection) -> str:
    queries = {
        "default privileges": """
            SELECT pg_get_userbyid(defaclrole), coalesce(defaclnamespace::regnamespace::text, '-'),
                   defaclobjtype, defaclacl::text
            FROM pg_default_acl ORDER BY 1, 2, 3""",
        "database": """
            SELECT datname, pg_get_userbyid(datdba), datacl::text, pg_encoding_to_char(encoding),
                   datcollate, datctype
            FROM pg_database WHERE datname = current_database()""",
        "role settings": """
            SELECT coalesce(d.datname, '-'), coalesce(r.rolname, '-'), s.setconfig::text
            FROM pg_db_role_setting s LEFT JOIN pg_database d ON d.oid = s.setdatabase
            LEFT JOIN pg_roles r ON r.oid = s.setrole ORDER BY 1, 2""",
        "extensions": "SELECT extname, extversion FROM pg_extension ORDER BY 1",
    }
    lines = []
    for title, query in queries.items():
        lines.append(f"## {title}")
        lines += ["\t".join(map(str, row)) for row in connection.execute(query)]
    return "\n".join(lines) + "\n"


def masked(value, window):
    """The value with migration-time timestamps and random UUIDs replaced by placeholders."""
    if isinstance(value, dict):
        return {k: masked(v, window) for k, v in value.items()}
    if isinstance(value, list):
        return [masked(v, window) for v in value]
    if isinstance(value, str):
        if UUID.match(value) and not value.startswith("00000000-"):
            return ("uuid", value)
        try:
            moment = datetime.fromisoformat(value)
        except ValueError:
            return value
        if moment.tzinfo is not None and window[0] <= moment <= window[1]:
            return "<migrated at>"
    return value


def rendered(value, labels):
    if isinstance(value, tuple):
        return labels.setdefault(value[1], f"<uuid {len(labels) + 1}>")
    if isinstance(value, dict):
        return {k: rendered(v, labels) for k, v in value.items()}
    if isinstance(value, list):
        return [rendered(v, labels) for v in value]
    return value


def data(connection: psycopg.Connection, window) -> str:
    tables = connection.execute(
        """SELECT n.nspname || '.' || c.relname FROM pg_class c
           JOIN pg_namespace n ON n.oid = c.relnamespace
           WHERE c.relkind IN ('r', 'p') AND n.nspname = ANY(%s) ORDER BY 1""",
        (list(SCHEMAS),),
    ).fetchall()
    sections = []
    for (table,) in tables:
        if table in SKIPPED:
            continue
        rows = [
            masked(row, window)
            for (row,) in connection.execute(f"SELECT row_to_json(t) FROM {table} t")
        ]
        # Sort with every random UUID alike, then number them in that order.
        anonymous = [json.dumps(rendered(r, {}), default=str) for r in rows]
        rows = [r for _, r in sorted(zip(anonymous, rows, strict=True), key=lambda p: p[0])]
        sections.append((table, rows))
    labels = {}
    lines = []
    for table, rows in sections:
        if rows:
            lines.append(f"## {table} ({len(rows)})")
            lines += [json.dumps(rendered(r, labels), default=str) for r in rows]
    sequences = connection.execute(
        """SELECT schemaname || '.' || sequencename, last_value FROM pg_sequences
           WHERE schemaname = ANY(%s) ORDER BY 1""",
        (list(SCHEMAS),),
    ).fetchall()
    lines.append("## sequences")
    lines += [f"{name}\t{value}" for name, value in sequences]
    return "\n".join(lines) + "\n"


def capture(out: Path, ref: str | None) -> None:
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="nx-ev-") as directory:
        workspace = Path(directory)
        source = source_tree(ref, workspace)
        services = NativeServices(workspace / "pg", database=DATABASE)
        try:
            services.start_postgres()
            with services.admin_connection() as connection:
                connection.execute("SET TimeZone = 'UTC'")
                start = connection.execute("SELECT clock_timestamp()").fetchone()[0]
                migrate(services, source)
                end = connection.execute("SELECT clock_timestamp()").fetchone()[0]
                (out / "catalog.txt").write_text(catalog(connection))
                (out / "data.txt").write_text(data(connection, (start, end)))
            (out / "schema.sql").write_text(dump(services, "pg_dump", "--schema-only", DATABASE))
            (out / "roles.sql").write_text(
                dump(services, "pg_dumpall", "--roles-only", "--no-role-passwords")
            )
        finally:
            services.stop_postgres()
    print(f"captured {ref or 'working tree'} in {out}")


def compare(a: Path, b: Path) -> bool:
    same = True
    for name in FILES:
        left, right = (a / name).read_text(), (b / name).read_text()
        if left == right:
            print(f"{name}: identical ({len(left.splitlines())} lines)")
            continue
        same = False
        diff = list(
            difflib.unified_diff(
                left.splitlines(True), right.splitlines(True), str(a / name), str(b / name)
            )
        )
        print(f"{name}: {sum(1 for d in diff if d[:1] in '+-') - 2} differing lines")
        sys.stdout.writelines(diff[:200])
    return same


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.partition("\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    one = commands.add_parser("capture", help="record one migration run")
    one.add_argument("out", type=Path)
    one.add_argument("--ref", help="git revision whose migrations run (default: working tree)")
    two = commands.add_parser("compare", help="compare two records")
    two.add_argument("a", type=Path)
    two.add_argument("b", type=Path)
    check = commands.add_parser("check", help="capture --ref and the working tree, then compare")
    check.add_argument("--ref", help="default: the revision the baseline replaced")
    check.add_argument("--keep", type=Path, help="keep both records in this directory")
    args = parser.parse_args()
    if args.command == "capture":
        capture(args.out, args.ref)
        return 0
    if args.command == "compare":
        return 0 if compare(args.a, args.b) else 1
    ref = args.ref or baseline_parent()
    with tempfile.TemporaryDirectory(prefix="nx-cmp-") as directory:
        keep = args.keep or Path(directory)
        capture(keep / "reference", ref)
        capture(keep / "working-tree", None)
        return 0 if compare(keep / "reference", keep / "working-tree") else 1


if __name__ == "__main__":
    sys.exit(main())
