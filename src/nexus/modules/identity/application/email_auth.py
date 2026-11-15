"""Public authentication policy, mailbox ownership proofs and recovery delivery."""

import logging
import secrets
from uuid import UUID, uuid4

from nexus.shared.policy import ImportPlan, PolicyRejected, changes, plan
from nexus.shared.telemetry import event, request_id

from ..domain.email import mailbox
from ..domain.errors import IdentityError, MailRejected, registration_unavailable, unauthorized
from ..domain.logins import new_login
from ..domain.tokens import digest_token
from .schemas import AuthPolicy, PublicAuthOptions, RegistrationCreated, VerificationPending

logger = logging.getLogger("nexus.mail")

# "exists" tells a mailbox owner that the login they tried is taken. Its token is never sent; the
# prefix only keeps the stored digest unique and unusable as any other proof.
PREFIXES = {"register": "nxv_", "bind": "nxb_", "reset": "nxr_", "exists": "nxe_"}
# Audit resource of system changes to the singleton authentication policy.
POLICY_RESOURCE = UUID(int=0)


class EmailAuthentication:
    """Shares the identity service authentication and transaction primitives."""

    async def public_options(self):
        policy = await self._read_policy()
        return PublicAuthOptions(
            registration=policy.registration,
            email_verification=policy.email_verification,
            turnstile=policy.turnstile,
            turnstile_site_key=self.providers.site_key if policy.turnstile else None,
            email_delivery_available=self.providers.mail_ready,
        )

    async def policy(self, session):
        async with self._access.authenticated(session) as (tx, actor):
            self._access.platform_admin(actor)
            return await tx.email.policy()

    def require_mail(self):
        if not self.providers.mail_ready:
            raise IdentityError("mail_unavailable", "Email delivery is not configured", 503)

    def _check_policy(self, policy, error):
        # Without verification the registration response would have to say whether a login
        # exists, and anyone could open accounts in bulk without owning a mailbox.
        if policy.registration and not policy.email_verification:
            raise error(
                "registration_requires_email_verification",
                "Enable email verification before opening registration",
                422,
            )
        if policy.email_verification and not self.providers.mail_ready:
            raise error("mail_unavailable", "Email delivery is not configured", 503)
        if policy.turnstile and not self.providers.turnstile_ready:
            raise error("turnstile_not_configured", "Configure Turnstile before enabling it", 409)

    async def update_policy(self, session, body, version):
        async with self._access.authenticated(session) as (tx, actor):
            self._access.platform_admin(actor)
            current = await tx.email.policy()
            if current.managed is not None:
                raise IdentityError(
                    "policy_managed_externally",
                    "The authentication policy is managed by a policy file",
                    409,
                )
            if current.version != version:
                raise IdentityError("version_conflict", "Authentication policy has changed", 412)
            self._check_policy(body, IdentityError)
            result = await tx.email.set_policy(body, version)
            if not result:
                raise IdentityError("version_conflict", "Authentication policy has changed", 412)
            await tx.audit.record(
                actor.user.id,
                "auth_policy.updated",
                actor.user.id,
                details=changes(
                    current.model_dump(mode="json", exclude={"version", "managed"}),
                    result.model_dump(mode="json", exclude={"version", "managed"}),
                ),
            )
        self._settings.put(result)
        return result

    async def plan_import(self, mode, values, file_sha256):
        """Plan the ``identity.auth_policy`` group of a policy file; nothing is written yet."""
        group = "identity.auth_policy"
        current = await self._read_policy()
        target, found, skipped = plan(
            group,
            mode,
            current.model_dump(mode="json", exclude={"version", "managed"}),
            current.version,
            current.managed is not None,
            values,
            AuthPolicy,
        )
        policy = AuthPolicy.model_validate(target)

        def rejected(code, message, status):
            return PolicyRejected(group, message)

        self._check_policy(policy, rejected)

        async def write(details):
            async with self._transactions() as tx:
                result = await tx.email.set_policy(
                    policy, current.version, managed=file_sha256 if mode == "managed" else None
                )
                if not result:
                    raise IdentityError(
                        "version_conflict", "Authentication policy has changed", 412
                    )
                await tx.audit.record_system(
                    "auth_policy.updated", POLICY_RESOURCE, {**details, **found}
                )
            self._settings.put(result)

        return ImportPlan(group, mode, mode == "managed", found, skipped, write if found else None)

    async def _read_policy(self):
        async with self._transactions() as tx:
            return await tx.email.policy()

    async def _challenge(self, policy, action, token):
        if policy.turnstile:
            await self.providers.verify(token, action)

    async def guard(self, action, token):
        policy = await self._read_policy()
        await self._challenge(policy, action, token)
        return policy.version

    async def current(self, tx, version, *, registration=False):
        policy = await tx.email.policy(share=True)
        if policy.version != version:
            raise IdentityError(
                "auth_policy_changed", "Authentication settings changed; retry", 409
            )
        if registration and not policy.registration:
            raise registration_unavailable()
        return policy

    async def _mail_budget(self, recipient, peer, account=None):
        """Spend one message from every budget it draws on, or refuse with Retry-After.

        Callers spend it only after Turnstile and any password check, so requests that fail those
        cannot drain the budgets that other requesters share.
        """
        try:
            budgets = (await self._settings.get()).mail
            wait = await self._mail_limits.allow(recipient, peer, account, budgets)
        except Exception:
            raise IdentityError(
                "authentication_unavailable", "Authentication is temporarily unavailable", 503
            ) from None
        if wait:
            raise IdentityError("rate_limited", "Too many email requests", 429, retry_after=wait)

    async def queue(self, tx, purpose, email, *, user_id=None, login=None, password_hash=None):
        self.require_mail()
        delivery_id = uuid4()
        prefix = PREFIXES[purpose]
        token = prefix + secrets.token_urlsafe(32)
        version, ciphertext = self.cipher.encrypt(token, user_id or delivery_id, delivery_id)
        if user_id:
            await tx.email.clear(user_id, [purpose])
        await tx.email.queue(
            dict(
                id=delivery_id,
                user_id=user_id,
                email=email,
                purpose=purpose,
                login=login,
                password_hash=password_hash,
                digest=digest_token(token, prefix),
                version=version,
                ciphertext=ciphertext,
                queue_ttl=(await self._settings.get()).mail.queue_ttl_seconds,
            )
        )

        return delivery_id

    def log_queued(self, job, purpose):
        event(
            logger,
            logging.INFO,
            "mail.queued",
            request_id=request_id.get(),
            job_id=job,
            purpose=purpose,
            provider=self.providers.name,
        )

    async def register(self, body, peer):
        # Closed registration answers before any other check, identically for every input.
        policy = await self._read_policy()
        if not policy.registration:
            raise registration_unavailable()
        await self._throttle("register:" + peer, peer)
        login = new_login(body.login, frozenset(policy.reserved_logins))
        address = mailbox(body.email if body.email is not None else login)
        self.require_mail()
        await self._challenge(policy, "register", body.turnstile_token)
        await self._mail_budget(address, peer, "login:" + login)
        password_hash = await self._hash(body.password.get_secret_value())
        async with self._transactions() as tx:
            await self.current(tx, policy.version, registration=True)
            # Whether or not the login is taken, the request does the same work and gets the same
            # answer; only the owner of the mailbox learns which message was sent.
            if await tx.administrators.login_exists(login):
                purpose = "exists"
                job = await self.queue(tx, purpose, address)
            else:
                purpose = "register"
                job = await self.queue(
                    tx, purpose, address, login=login, password_hash=password_hash
                )
        self.log_queued(job, purpose)
        return VerificationPending()

    async def finish_registration(self, tx, login, password_hash, email):
        user = await tx.directory.create_user(login, password_hash)
        if email:
            await tx.email.bind(user.id, email)
        org = await tx.directory.personal_org_id(user.id)
        await tx.audit.record(user.id, "user.registered", user.id, organization_id=org)
        session = await self._new_session(tx, user.id)
        return RegistrationCreated(**session.model_dump(), personal_organization_id=org), user.id

    async def confirm_registration(self, body, peer):
        digest = digest_token(body.token.get_secret_value(), "nxv_")
        await self._throttle("confirm:" + digest, peer)
        version = await self.guard("confirm_registration", body.turnstile_token)
        async with self._transactions() as tx:
            pending = await tx.email.find(digest, "register")
        if not pending or not await self._verify(
            pending["password_hash"], body.password.get_secret_value()
        ):
            raise unauthorized()
        async with self._transactions() as tx:
            await self.current(tx, version, registration=True)
            row = await tx.email.consume(digest, "register")
            if not row:
                raise unauthorized()
            result, _ = await self.finish_registration(
                tx, row["login"], row["password_hash"], row["email"]
            )

            return result

    async def request_binding(self, session, body, peer):
        address = mailbox(body.email)
        await self._throttle("bind:" + peer, peer)
        version = await self.guard("bind_email", body.turnstile_token)
        self.require_mail()
        async with self._access.authenticated(session) as (tx, actor):
            credentials = await tx.directory.credentials(actor.user.login)
        password_hash = credentials[1]
        if not await self._verify(password_hash, body.password.get_secret_value()):
            raise unauthorized()
        await self._mail_budget(address, peer, f"user:{actor.user.id}")
        async with self._access.authenticated(session) as (tx, actor):
            await self.current(tx, version)
            await tx.directory.user(actor.user.id, lock=True)
            if not await tx.directory.password_matches(actor.user.id, password_hash):
                raise unauthorized()
            job = await self.queue(tx, "bind", address, user_id=actor.user.id)
        self.log_queued(job, "bind")

    async def confirm_binding(self, session, body, peer):
        digest = digest_token(body.token.get_secret_value(), "nxb_")
        await self._throttle("bind-confirm:" + digest, peer)
        version = await self.guard("confirm_email", body.turnstile_token)
        async with self._access.authenticated(session) as (tx, actor):
            await self.current(tx, version)
            await tx.directory.user(actor.user.id, lock=True)
            row = await tx.email.consume(digest, "bind")
            if not row or row["user_id"] != actor.user.id:
                raise unauthorized()
            await tx.email.bind(actor.user.id, row["email"])
            await tx.email.clear(actor.user.id, ["reset"])
            await tx.audit.record(actor.user.id, "user.email_verified", actor.user.id)

    async def forgot(self, body, peer):
        address = mailbox(body.email)
        await self._throttle("forgot:" + peer, peer)
        version = await self.guard("forgot_password", body.turnstile_token)
        self.require_mail()
        # Spent before the lookup, so the answer and the limits are the same for unknown
        # addresses and for accounts that may not recover by email.
        await self._mail_budget(address, peer)
        async with self._transactions() as tx:
            policy = await self.current(tx, version)
            user_id = await tx.email.recipient(
                address, platform_recovery=policy.platform_email_recovery
            )
            if not user_id:
                return
            job = await self.queue(tx, "reset", address, user_id=user_id)
        self.log_queued(job, "reset")

    async def reset(self, body, peer):
        digest = digest_token(body.token.get_secret_value(), "nxr_")
        await self._throttle("reset:" + digest, peer)
        version = await self.guard("reset_password", body.turnstile_token)
        async with self._transactions() as tx:
            proof = await tx.email.find(digest, "reset")
        if not proof:
            raise unauthorized()
        password_hash = await self._hash(body.password.get_secret_value())
        async with self._transactions() as tx:
            policy = await self.current(tx, version)
            user = await tx.directory.user(proof["user_id"], lock=True)
            row = await tx.email.consume(digest, "reset")
            if (
                not row
                or not user
                or user.status != "active"
                or (await tx.email.verified(user.id) != row["email"])
            ):
                raise unauthorized()
            # A platform role granted after the token was sent still closes this path.
            if not policy.platform_email_recovery and (await tx.directory.roles(user.id)).roles:
                raise unauthorized()
            await tx.directory.replace_password(user.id, password_hash)
            await tx.audit.record(user.id, "user.password_reset", user.id)

    async def dispatch_email(self):
        lease = uuid4()
        async with self._transactions() as tx:
            job, expired = await tx.email.claim(lease)
        for row in expired:
            event(
                logger,
                logging.WARNING,
                "mail.expired",
                request_id=row["trace_id"],
                job_id=row["id"],
                purpose=row["purpose"],
                attempt=row["attempts"],
            )
        if not job:
            return 0
        fields = dict(
            request_id=job["trace_id"],
            job_id=job["id"],
            purpose=job["purpose"],
            provider=self.providers.name,
            attempt=job["attempts"],
        )
        event(logger, logging.INFO, "mail.attempt", **fields)
        try:
            token = self.cipher.decrypt(
                job["ciphertext"], job["user_id"] or job["id"], job["id"], job["key_version"]
            )
            status = await self.providers.send(job["email"], job["purpose"], token, job["id"])
        except MailRejected as error:
            # The provider answered and refused this message. Retrying cannot help, and the
            # provider itself is reachable, so the worker stays ready.
            async with self._transactions() as tx:
                dropped = await tx.email.reject(job["id"], lease)
            event(
                logger,
                logging.WARNING,
                "mail.failed",
                **fields,
                error_type=type(error).__name__,
                provider_status=error.status,
                outcome="rejected" if dropped else "superseded",
            )
            return 1
        except Exception as error:
            async with self._transactions() as tx:
                scheduled = await tx.email.finish(job["id"], lease, False)
            event(
                logger,
                logging.WARNING,
                "mail.failed",
                **fields,
                error_type=type(error).__name__,
                provider_status=self.providers.failure_status(error),
                outcome="retry_scheduled" if scheduled else "superseded",
            )
            raise
        async with self._transactions() as tx:
            finished = await tx.email.finish(job["id"], lease, True)
        event(
            logger,
            logging.INFO,
            "mail.accepted",
            **fields,
            provider_status=status,
            outcome="recorded" if finished else "superseded",
        )
        return 1
