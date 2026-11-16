"""Static checks on the migration sources."""

import re
from pathlib import Path

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"


def test_references_are_granted_through_the_locking_helper():
    """A bare GRANT REFERENCES on an existing table can deadlock the revision with ANALYZE.

    ``nexus.infrastructure.migrations.references`` locks the tables before granting.
    """
    sources = [*MIGRATIONS.glob("versions/*.py"), *MIGRATIONS.glob("sql/**/*.sql")]
    bare = [
        str(path.relative_to(MIGRATIONS))
        for path in sources
        if re.search(r"\b(GRANT|REVOKE)\s+REFERENCES\b", path.read_text(), re.IGNORECASE)
    ]
    assert sources and bare == []
