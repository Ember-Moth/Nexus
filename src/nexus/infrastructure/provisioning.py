"""Database roles and grants a deployment prepares before the first migration.

The repository-local services (``scripts/native_services.py``) and the Kubernetes deployment
(``nexus provision-database``, ``deploy/helm``) run the same steps. Every step is idempotent: an
existing role keeps its attributes, so roles that an operator (for example CloudNativePG managed
roles) already declared are only granted what is missing.
"""

from collections.abc import Callable, Mapping

import psycopg
from psycopg import sql

MODULES = ("identity", "catalog", "billing", "publication", "reporting")
# Group roles own the module schemas and the gateway functions; nobody logs in as them.
GROUP_ROLES = tuple(f"nexus_{name}" for name in (*MODULES, "bridge", "gateway"))
# Process logins. They switch to module roles inside each transaction, hence NOINHERIT.
LOGIN_ROLES = ("nexus_api", "nexus_worker", "nexus_migrator")
MINIMUM_SERVER_VERSION = 160000

Connect = Callable[[str], psycopg.Connection]


class ProvisioningError(RuntimeError):
    pass


def _exists(connection: psycopg.Connection, role: str) -> bool:
    return bool(connection.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone())


def provision(
    connect: Connect,
    database: str,
    passwords: Mapping[str, str] | None = None,
    *,
    maintenance_database: str = "postgres",
) -> None:
    """Create missing roles and the database, then apply the grants every deployment needs.

    ``connect(name)`` returns an autocommit connection to database ``name`` as a role that may
    create roles and databases. ``passwords`` maps ``nexus_api`` and the other login roles to the
    password they are created with, or changed to when they already exist; a role that is missing
    and has no password is an error, because the processes could not log in.
    """
    passwords = passwords or {}
    with connect(maintenance_database) as connection:
        if connection.info.server_version < MINIMUM_SERVER_VERSION:
            raise ProvisioningError("PostgreSQL 16 or newer is required")
        for role in GROUP_ROLES:
            if not _exists(connection, role):
                connection.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(role)))
        for role in LOGIN_ROLES:
            password = passwords.get(role)
            if not _exists(connection, role):
                if not password:
                    raise ProvisioningError(f"role {role} is missing and has no password")
                connection.execute(
                    sql.SQL(
                        "CREATE ROLE {} LOGIN NOINHERIT NOSUPERUSER "
                        "NOCREATEDB NOCREATEROLE PASSWORD {}"
                    ).format(sql.Identifier(role), sql.Literal(password))
                )
            elif password:
                connection.execute(
                    sql.SQL("ALTER ROLE {} PASSWORD {}").format(
                        sql.Identifier(role), sql.Literal(password)
                    )
                )
        if not connection.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (database,)
        ).fetchone():
            connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
        name = sql.Identifier(database)
        connection.execute(sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(name))
        connection.execute(
            sql.SQL(
                "GRANT CONNECT ON DATABASE {} TO nexus_api, nexus_worker, nexus_migrator"
            ).format(name)
        )
        connection.execute(sql.SQL("GRANT CREATE ON DATABASE {} TO nexus_migrator").format(name))
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
        connection.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO nexus_gateway").format(name))
    with connect(database) as connection:
        connection.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")


def ensure_gateway_login(connection: psycopg.Connection, role: str, password: str) -> bool:
    """Create or re-key one gateway node's login. Returns True when the role was created.

    A node login inherits ``nexus_gateway`` and nothing else; an existing role with other
    memberships or attributes is refused rather than silently narrowed.
    """
    if not role.startswith("nexus_") or not role.replace("_", "").isalnum():
        raise ProvisioningError("gateway node roles are lower-case identifiers prefixed nexus_")
    row = connection.execute(
        """SELECT r.rolcanlogin, r.rolsuper OR r.rolcreaterole OR r.rolcreatedb OR r.rolreplication
                  OR r.rolbypassrls,
                  ARRAY(SELECT g.rolname FROM pg_auth_members m JOIN pg_roles g ON g.oid=m.roleid
                        WHERE m.member=r.oid ORDER BY g.rolname)
             FROM pg_roles r WHERE r.rolname=%s""",
        (role,),
    ).fetchone()
    if row is None:
        connection.execute(
            sql.SQL("CREATE ROLE {} LOGIN INHERIT PASSWORD {} IN ROLE nexus_gateway").format(
                sql.Identifier(role), sql.Literal(password)
            )
        )
        return True
    login, privileged, memberships = row
    if not login or privileged or memberships != ["nexus_gateway"]:
        raise ProvisioningError(f"role {role} exists but is not a plain gateway node login")
    connection.execute(
        sql.SQL("ALTER ROLE {} PASSWORD {}").format(sql.Identifier(role), sql.Literal(password))
    )
    return False
