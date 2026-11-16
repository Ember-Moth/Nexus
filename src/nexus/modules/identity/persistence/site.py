from sqlalchemy import text

from nexus.shared.views import managed_source

from ..application.schemas import SitePolicyView

SITE_FIELDS = ("site_name", "console_url", "contact_email")
SITE_COLUMNS = ",".join(
    (*SITE_FIELDS, "setup_completed_at", "version", "managed_sha256", "managed_at")
)
# Sentinel: an administrator's write leaves the lock as it is (it is refused while set).
KEEP = object()


def site_view(row) -> SitePolicyView:
    return SitePolicyView(
        **{name: row[name] for name in (*SITE_FIELDS, "setup_completed_at", "version")},
        managed=managed_source(row),
    )


class SiteRepository:
    """The singleton ``identity.site_policy`` row: site description and setup state."""

    def __init__(self, session):
        self.session = session

    async def _one(self, statement: str, **params):
        row = (await self.session.execute(text(statement), params)).mappings().one_or_none()
        return site_view(row) if row else None

    async def policy(self, *, lock: bool = False) -> SitePolicyView:
        return await self._one(
            f"SELECT {SITE_COLUMNS} FROM identity.site_policy WHERE singleton"
            + (" FOR UPDATE" if lock else "")
        )

    async def set_info(self, info, version: int, *, managed=KEEP) -> SitePolicyView | None:
        """Store the site description if the stored version is still ``version``.

        ``managed`` is the SHA-256 of the policy file that locks the row, or None to unlock it.
        """
        lock = (
            ""
            if managed is KEEP
            else ",managed_sha256=:managed,managed_at=CASE WHEN CAST(:managed AS text) IS NULL "
            "THEN NULL ELSE clock_timestamp() END"
        )
        params = {name: getattr(info, name) for name in SITE_FIELDS}
        if managed is not KEEP:
            params["managed"] = managed
        return await self._one(
            f"""UPDATE identity.site_policy SET site_name=:site_name,console_url=:console_url,
                contact_email=:contact_email,version=version+1{lock}
            WHERE singleton AND version=:version RETURNING {SITE_COLUMNS}""",
            **params,
            version=version,
        )

    async def complete(self, info) -> SitePolicyView:
        """Record the finished wizard together with its site description."""
        return await self._one(
            f"""UPDATE identity.site_policy SET site_name=:site_name,console_url=:console_url,
                contact_email=:contact_email,setup_completed_at=clock_timestamp(),
                version=version+1
            WHERE singleton RETURNING {SITE_COLUMNS}""",
            **{name: getattr(info, name) for name in SITE_FIELDS},
        )

    async def reopen(self) -> SitePolicyView:
        return await self._one(
            f"""UPDATE identity.site_policy SET setup_completed_at=NULL,version=version+1
            WHERE singleton RETURNING {SITE_COLUMNS}"""
        )
