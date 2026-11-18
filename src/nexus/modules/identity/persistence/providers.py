from sqlalchemy import text

from ..domain.delivery import StoredMail, StoredTurnstile

MAIL_FIELDS = (
    "provider",
    "sender",
    "smtp_host",
    "smtp_port",
    "smtp_security",
    "smtp_username",
    "cloudflare_account_id",
    "credential",
    "key_version",
)
TURNSTILE_FIELDS = ("site_key", "hostname", "credential", "key_version")
META = ("version", "managed_sha256", "managed_at")
# The identity policy rows, in the order every transaction that writes one of them locks them.
LOCK_ORDER = ("auth_policy", "site_policy", "mail_policy", "turnstile_policy")
# Sentinel: an administrator's write leaves the lock as it is (it is refused while set).
KEEP = object()


class ProviderRepository:
    """The singleton ``identity.mail_policy`` and ``identity.turnstile_policy`` rows."""

    def __init__(self, session):
        self.session = session

    async def lock_policies(self, *written: str) -> None:
        """Lock every identity policy row in one order: the ``written`` ones for update.

        Writes check one policy against the others (a switch against its provider, the console
        address against the Turnstile hostname); the fixed order keeps those checks free of
        deadlocks.
        """
        for table in LOCK_ORDER:
            mode = "UPDATE" if table in written else "SHARE"
            await self.session.execute(
                text(f"SELECT 1 FROM identity.{table} WHERE singleton FOR {mode}")
            )

    async def _row(self, statement: str, **params):
        return (await self.session.execute(text(statement), params)).mappings().one_or_none()

    async def mail(self) -> StoredMail:
        row = await self._row(
            f"SELECT {','.join((*MAIL_FIELDS, *META))} FROM identity.mail_policy WHERE singleton"
        )
        return StoredMail(**row)

    async def turnstile(self) -> StoredTurnstile:
        columns = ",".join((*TURNSTILE_FIELDS, *META))
        row = await self._row(f"SELECT {columns} FROM identity.turnstile_policy WHERE singleton")
        return StoredTurnstile(**row)

    async def _set(self, table, fields, values, version, managed):
        """Store ``values`` if the stored version is still ``version``.

        ``managed`` is the SHA-256 of the policy file that locks the row, or None to unlock it.
        """
        assignments = [f"{name}=:{name}" for name in fields]
        params = {name: values[name] for name in fields}
        if managed is not KEEP:
            assignments.append(
                "managed_sha256=:managed,managed_at=CASE WHEN CAST(:managed AS text) IS NULL "
                "THEN NULL ELSE clock_timestamp() END"
            )
            params["managed"] = managed
        return await self._row(
            f"""UPDATE identity.{table} SET {",".join(assignments)},version=version+1
            WHERE singleton AND version=:version RETURNING {",".join((*fields, *META))}""",
            **params,
            version=version,
        )

    async def set_mail(self, values: dict, version: int, *, managed=KEEP) -> StoredMail | None:
        row = await self._set("mail_policy", MAIL_FIELDS, values, version, managed)
        return StoredMail(**row) if row else None

    async def set_turnstile(
        self, values: dict, version: int, *, managed=KEEP
    ) -> StoredTurnstile | None:
        row = await self._set("turnstile_policy", TURNSTILE_FIELDS, values, version, managed)
        return StoredTurnstile(**row) if row else None
