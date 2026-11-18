"""The identity groups of a policy file, planned and written together.

The site, authentication, mail and Turnstile policies depend on each other: a switch needs its
provider, and the Turnstile hostname defaults to the console host. A file may change both sides
at once, so the groups are checked as the file would leave them, and written in one transaction
that checks them again under the same row locks as an administrator's write.

Secrets never appear in a policy file. Their fields hold a reference that the importing host
resolves (``secret(group, path, reference)``, supplied by the command), or null to remove them.
"""

from collections.abc import Callable

from pydantic import SecretStr

from nexus.shared.policy import (
    MODES,
    ImportPlan,
    PolicyRejected,
    changes,
    merged,
    plan,
    validated,
)

from ..domain.delivery import MAIL_PROVIDERS, StoredMail, StoredTurnstile, console_host
from ..domain.errors import IdentityError
from .delivery import (
    MAIL_GROUP,
    MAIL_RESOURCE,
    SECRET_FIELDS,
    TURNSTILE_GROUP,
    TURNSTILE_RESOURCE,
    mail_facts,
    mail_values,
    stored_row,
    turnstile_facts,
    turnstile_values,
)
from .email_auth import POLICY_RESOURCE
from .schemas import AuthPolicy, MailSettings, SiteInfo, TurnstileSettings
from .setup import SITE_GROUP, SITE_RESOURCE, site_values

AUTH_GROUP = "identity.auth_policy"
# Planned and written in this order, which is also the order their rows are locked in.
GROUPS = (AUTH_GROUP, SITE_GROUP, MAIL_GROUP, TURNSTILE_GROUP)
TABLES = {
    AUTH_GROUP: "auth_policy",
    SITE_GROUP: "site_policy",
    MAIL_GROUP: "mail_policy",
    TURNSTILE_GROUP: "turnstile_policy",
}
SECRET_PATHS = {
    MAIL_GROUP: tuple((name, field) for name, field in SECRET_FIELDS.items()),
    TURNSTILE_GROUP: (("secret_key",),),
}
type Secret = Callable[[str, str, object], str]


def rejected(group):
    def error(code, message, status):
        return PolicyRejected(group, message)

    return error


def resolve(group: str, values: dict, secret: Secret) -> dict:
    """Replace each secret reference of ``values`` with the value it names on this host."""
    values = {k: dict(v) if isinstance(v, dict) else v for k, v in values.items()}
    for path in SECRET_PATHS[group]:
        node = values
        for key in path[:-1]:
            node = node.get(key) if isinstance(node, dict) else None
        if not isinstance(node, dict) or path[-1] not in node:
            continue
        reference = node[path[-1]]
        if reference is not None:
            node[path[-1]] = SecretStr(secret(group, ".".join(path), reference))
    return values


class Planned:
    """One group's plan: the change it makes and how to store it."""

    def __init__(self, group, mode, managed, found, skipped, apply, target, version):
        self.group, self.mode, self.managed = group, mode, managed
        self.found, self.skipped, self.apply = found, skipped, apply
        self.target, self.version = target, version


class PolicyImports:
    """Shares the identity service transaction primitives and provider settings."""

    async def plan_imports(self, entries: dict, file_sha256: str, secret: Secret):
        """Plan the identity groups of a file; the first changed group's ``write`` writes all."""
        async with self._transactions() as tx:
            current = {
                AUTH_GROUP: await tx.email.policy(),
                SITE_GROUP: await tx.site.policy(),
                MAIL_GROUP: await tx.providers.mail(),
                TURNSTILE_GROUP: await tx.providers.turnstile(),
            }
        planned: dict[str, Planned] = {}
        for group in GROUPS:
            if group not in entries:
                continue
            mode, values = entries[group]["mode"], entries[group].get("values") or {}
            if mode not in MODES:
                raise PolicyRejected(group, "mode must be initial or managed")
            if group == AUTH_GROUP:
                planned[group] = self._plan_auth(mode, values, current[group])
            elif group == SITE_GROUP:
                planned[group] = self._plan_site(mode, values, current[group])
            elif group == MAIL_GROUP:
                planned[group] = self._plan_mail(mode, resolve(group, values, secret), current)
            else:
                console = (
                    planned[SITE_GROUP].target.console_url
                    if SITE_GROUP in planned
                    else current[SITE_GROUP].console_url
                )
                planned[group] = self._plan_turnstile(
                    mode, resolve(group, values, secret), current[group], console
                )
        self._check_imported(planned, current)
        changed = [group for group in GROUPS if group in planned and planned[group].found]

        async def write(details):
            await self._write_imported(
                [planned[group] for group in changed], current, file_sha256, details
            )

        return [
            ImportPlan(
                group,
                planned[group].mode,
                planned[group].mode == "managed",
                planned[group].found,
                planned[group].skipped,
                write if changed and group == changed[0] else None,
            )
            for group in GROUPS
            if group in planned
        ]

    def _plan_auth(self, mode, values, current) -> Planned:
        target, found, skipped = plan(
            AUTH_GROUP,
            mode,
            current.model_dump(mode="json", exclude={"version", "managed"}),
            current.version,
            current.managed is not None,
            values,
            AuthPolicy,
        )
        return Planned(
            AUTH_GROUP,
            mode,
            current.managed is not None,
            found,
            skipped,
            True,
            AuthPolicy.model_validate(target),
            current.version,
        )

    def _plan_site(self, mode, values, current) -> Planned:
        target, found, skipped = plan(
            SITE_GROUP,
            mode,
            site_values(current),
            current.version,
            current.managed is not None,
            values,
            SiteInfo,
        )
        return Planned(
            SITE_GROUP,
            mode,
            current.managed is not None,
            found,
            skipped,
            True,
            SiteInfo.model_validate(target),
            current.version,
        )

    def _plan_secret_group(self, group, mode, stored, proposed_row, replaced, facts) -> Planned:
        pristine = stored.version == 1
        apply = mode == "managed" or pristine
        before = facts(stored_row(stored))
        after = facts(proposed_row, replaced=replaced)
        found = changes(before, after) if apply else {}
        managed = stored.managed_sha256 is not None
        if managed != (mode == "managed"):
            found["managed"] = [managed, mode == "managed"]
        skipped = mode == "initial" and not pristine and bool(changes(before, after))
        target = proposed_row if apply else stored_row(stored)
        return Planned(group, mode, managed, found, skipped, apply, target, stored.version)

    def _plan_mail(self, mode, values, current) -> Planned:
        stored: StoredMail = current[MAIL_GROUP]
        proposed = merged(mail_values(stored), values)
        # Sections of the stored provider go with it unless the file names them.
        for name in MAIL_PROVIDERS:
            if name != proposed.get("provider") and name not in values:
                proposed[name] = None
        if proposed.get("provider") is None and "sender" not in values:
            proposed["sender"] = None
        body = validated(MAIL_GROUP, MailSettings, proposed)
        row, replaced = self._mail_target(body, stored, rejected(MAIL_GROUP))
        return self._plan_secret_group(MAIL_GROUP, mode, stored, row, replaced, mail_facts)

    def _plan_turnstile(self, mode, values, stored: StoredTurnstile, console_url) -> Planned:
        proposed = merged(turnstile_values(stored), values)
        if proposed.get("site_key") is None and "hostname" not in values:
            proposed["hostname"] = None
        body = validated(TURNSTILE_GROUP, TurnstileSettings, proposed)
        row, replaced = self._turnstile_target(body, stored, console_url, rejected(TURNSTILE_GROUP))
        return self._plan_secret_group(
            TURNSTILE_GROUP, mode, stored, row, replaced, turnstile_facts
        )

    def _check_imported(self, planned: dict, current: dict) -> None:
        """Every switch the file leaves on has a usable provider once the file is applied."""
        auth = planned[AUTH_GROUP].target if AUTH_GROUP in planned else current[AUTH_GROUP]
        console = (
            planned[SITE_GROUP].target.console_url
            if SITE_GROUP in planned
            else current[SITE_GROUP].console_url
        )
        if MAIL_GROUP in planned:
            mail_ready = planned[MAIL_GROUP].target["provider"] is not None
        else:
            mail_ready = self._mail_transport(current[MAIL_GROUP]) is not None
        if TURNSTILE_GROUP in planned:
            row = planned[TURNSTILE_GROUP].target
            turnstile_ready = row["site_key"] is not None and bool(
                row["hostname"] or console_host(console)
            )
        else:
            turnstile_ready = self._turnstile_keys(current[TURNSTILE_GROUP], console) is not None

        def blame(provider_group):
            return provider_group if provider_group in planned else AUTH_GROUP

        before = current[AUTH_GROUP]
        if auth.registration and not auth.email_verification:
            raise PolicyRejected(AUTH_GROUP, "enable email verification before registration")
        # A switch that stays on is checked only against a provider the file changes.
        mail_checked = MAIL_GROUP in planned or not before.email_verification
        if auth.email_verification and mail_checked and not mail_ready:
            raise PolicyRejected(
                blame(MAIL_GROUP), f"email verification needs a configured {MAIL_GROUP}"
            )
        turnstile_checked = (
            TURNSTILE_GROUP in planned or SITE_GROUP in planned or not before.turnstile
        )
        if auth.turnstile and turnstile_checked and not turnstile_ready:
            group = blame(TURNSTILE_GROUP)
            if TURNSTILE_GROUP not in planned and SITE_GROUP in planned:
                group = SITE_GROUP
            raise PolicyRejected(
                group,
                f"Turnstile needs {TURNSTILE_GROUP} with a hostname or a site console URL",
            )

    async def _write_imported(self, plans: list[Planned], current, file_sha256, details) -> None:
        if not plans:
            return
        written = {}
        async with self._transactions() as tx:
            await tx.providers.lock_policies(*(TABLES[p.group] for p in plans))
            for item in plans:
                managed = file_sha256 if item.mode == "managed" else None
                if item.group == AUTH_GROUP:
                    result = await tx.email.set_policy(item.target, item.version, managed=managed)
                    action, resource = "auth_policy.updated", POLICY_RESOURCE
                elif item.group == SITE_GROUP:
                    result = await tx.site.set_info(item.target, item.version, managed=managed)
                    action, resource = "site_policy.updated", SITE_RESOURCE
                elif item.group == MAIL_GROUP:
                    result = await tx.providers.set_mail(item.target, item.version, managed=managed)
                    action, resource = "mail_policy.updated", MAIL_RESOURCE
                else:
                    result = await tx.providers.set_turnstile(
                        item.target, item.version, managed=managed
                    )
                    action, resource = "turnstile_policy.updated", TURNSTILE_RESOURCE
                if not result:
                    raise IdentityError(
                        "version_conflict", f"{item.group} changed during the import", 412
                    )
                written[item.group] = result
                await tx.audit.record_system(
                    action, resource, {**details, "mode": item.mode, **item.found}
                )
            # An administrator may have changed another identity policy since the plan.
            policy = await tx.email.policy()
            site = await tx.site.policy()
            mail = self._mail_transport(await tx.providers.mail())
            keys = self._turnstile_keys(await tx.providers.turnstile(), site.console_url)
            if (policy.email_verification and mail is None) or (policy.turnstile and keys is None):
                raise IdentityError(
                    "version_conflict", "Identity policies changed during the import", 412
                )
        if AUTH_GROUP in written:
            self._settings.put(written[AUTH_GROUP])
        if SITE_GROUP in written:
            self._site.put(written[SITE_GROUP])
        self._providers.invalidate()
