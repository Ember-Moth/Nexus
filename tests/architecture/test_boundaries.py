import ast
from importlib.util import resolve_name
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

from nexus.shared.storage import SCHEMA_REVISION

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
ENTRY_POINTS = {"nexus.cli", "nexus.api", "nexus.worker", "nexus.commands", "nexus.bootstrap"}


def public_exports():
    result = {}
    for path in (SRC / "nexus/modules").glob("*/__init__.py"):
        exports = set()
        for node in ast.parse(path.read_text()).body:
            if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "__all__" for target in node.targets
            ):
                exports.update(ast.literal_eval(node.value))
        result[path.parent.name] = exports
    return result


PUBLIC_EXPORTS = public_exports()


def imports(source: str, module: str, is_package: bool = False):
    package = module if is_package else module.rpartition(".")[0]
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            target = node.module or ""
            if node.level:
                target = resolve_name("." * node.level + target, package)
            for alias in node.names:
                yield target + "." + alias.name


def violations(source: str, module: str, is_package: bool = False) -> list[str]:
    failures = []
    parts = module.split(".")
    owner = parts[2] if module.startswith("nexus.modules.") else None
    kind = parts[-1]
    http_entry = kind in {"api", "routes"}
    persistence = kind in {"repository", "models"} or kind.endswith("_repository")
    for target in imports(source, module, is_package):
        if target.startswith("nexus.bootstrap") and module not in ENTRY_POINTS:
            failures.append("business code depends on process assembly")
        if target.startswith(("nexus.models", "nexus.contracts", "nexus.repository")):
            failures.append("global business data store or DTO package")
        if target.startswith("nexus.modules."):
            imported = target.split(".")
            foreign = not owner or imported[2] != owner
            if (
                foreign
                and len(imported) > 3
                and imported[3] not in PUBLIC_EXPORTS.get(imported[2], set())
            ):
                failures.append("private module import")
            if module.startswith(("nexus.shared.", "nexus.infrastructure.", "nexus.config")):
                failures.append("foundation depends on business module")
        if owner:
            if target.startswith(("nexus.api", "nexus.worker", "nexus.commands", "nexus.cli")):
                failures.append("module depends on process entry point")
            if not http_entry and target.startswith(
                ("fastapi", "starlette", "uvicorn", "nexus.http")
            ):
                failures.append("business core depends on HTTP framework")
            if target.startswith(("sqlalchemy", "psycopg", "redis")) and not persistence:
                failures.append("business core bypasses its persistence boundary")
            if target.startswith("nexus.infrastructure") and not (is_package or persistence):
                failures.append("business core bypasses its ports")
        if (module in {"nexus.api", "nexus.worker"} or http_entry and owner) and target.startswith(
            ("sqlalchemy", "psycopg", "nexus.infrastructure.postgres")
        ):
            failures.append("entry point opens business storage")
        if (
            owner
            and kind == "schemas"
            and (
                target.startswith("sqlalchemy")
                or (
                    target.startswith("nexus.modules.")
                    and (
                        target.split(".")[2] != owner
                        or target.startswith(
                            (
                                f"nexus.modules.{owner}.models",
                                f"nexus.modules.{owner}.repository",
                            )
                        )
                    )
                )
            )
        ):
            failures.append("schema exposes another module or ORM")
    for node in ast.walk(ast.parse(source)):
        if owner and isinstance(node, ast.Call):
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name in {"__import__", "import_module"}:
                failures.append("dynamic module import bypasses dependency checks")
    return failures


def test_source_boundaries():
    failures = []
    for path in (SRC / "nexus").rglob("*.py"):
        module = ".".join(path.relative_to(SRC).with_suffix("").parts)
        package = path.name == "__init__.py"
        if package:
            module = module.removesuffix(".__init__")
        failures.extend(
            f"{path.relative_to(ROOT)}: {v}" for v in violations(path.read_text(), module, package)
        )
    assert not failures, "\n".join(failures)


@pytest.mark.parametrize(
    "module, source",
    [
        ("nexus.modules.billing.service", "from nexus.bootstrap import Application"),
        ("nexus.modules.billing.service", "from nexus.modules.identity import repository"),
        (
            "nexus.modules.billing.service",
            "from nexus.modules.identity.schemas import BootstrapResult",
        ),
        ("nexus.modules.billing.service", "from fastapi import Depends"),
        ("nexus.modules.billing.schemas", "from .models import Account"),
        ("nexus.modules.billing.service", "from sqlalchemy import select"),
        ("nexus.infrastructure.cache", "from nexus.modules import billing"),
        ("nexus.worker", "from sqlalchemy import text"),
        ("nexus.modules.billing.service", "from ...api import create_app"),
        ("nexus.modules.billing.service", '__import__("nexus.bootstrap")'),
    ],
)
def test_guard_rejects_invalid_dependencies(module, source):
    assert violations(source, module)


@pytest.mark.parametrize(
    "module, source",
    [
        ("nexus.bootstrap", "from nexus.modules import identity"),
        ("nexus.modules.billing.service", "from nexus.modules.identity import BootstrapResult"),
        ("nexus.modules.identity.api", "from fastapi import APIRouter"),
        ("nexus.modules.identity.repository", "from sqlalchemy import select"),
        ("nexus.modules.billing.schemas", "from .money import Money"),
    ],
)
def test_public_interfaces_and_local_layers_are_allowed(module, source):
    assert not violations(source, module)


def test_readiness_revision_matches_migration_head():
    scripts = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))
    assert scripts.get_heads() == [SCHEMA_REVISION]
