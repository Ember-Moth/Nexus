"""Public authentication policy, mailbox ownership proofs and recovery delivery."""

import logging
import secrets
from uuid import uuid4

from nexus.shared.telemetry import event, request_id

from ..domain.email import mailbox
from ..domain.errors import IdentityError, unauthorized
from ..domain.tokens import digest_token
from .schemas import PublicAuthOptions, RegistrationCreated, VerificationPending

logger = logging.getLogger("nexus.mail")


class EmailAuthentication:
    """Shares the identity service authentication and transaction primitives."""

    async def public_options(self):
        async with self._transactions() as tx:
            policy = await tx.email.policy()
        return PublicAuthOptions(
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

    async def update_policy(self, session, body, version):
        async with self._access.authenticated(session) as (tx, actor):
            self._access.platform_admin(actor)
            if body.email_verification:
                self.require_mail()
            if body.turnstile and not self.providers.turnstile_ready:
                raise IdentityError(
                    "turnstile_not_configured", "Configure Turnstile before enabling it", 409
                )
            result = await tx.email.set_policy(body, version)
            if not result:
                raise IdentityError("version_conflict", "Authentication policy has changed", 412)
            await tx.audit.record(
                actor.user.id, "auth_policy.updated", actor.user.id, details=body.model_dump()
            )
            return result

    async def guard(self, action, token):
        async with self._transactions() as tx:
            policy = await tx.email.policy()
        if policy.turnstile:
            await self.providers.verify(token, action)
        return policy.version

    async def current(self, tx, version):
        policy = await tx.email.policy(share=True)
        if policy.version != version:
            raise IdentityError(
                "auth_policy_changed", "Authentication settings changed; retry", 409
            )
        return policy

    async def queue(self, tx, purpose, email, *, user_id=None, login=None, password_hash=None):
        self.require_mail()
        delivery_id = uuid4()
        prefix = {"register": "nxv_", "bind": "nxb_", "reset": "nxr_"}[purpose]
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

    async def _register(self, body, login, peer):
        address = mailbox(body.email) if body.email is not None else None
        await self._throttle("register:" + (address or login), peer)
        version = await self.guard("register", body.turnstile_token)
        password_hash = await self._hash(body.password.get_secret_value())
        async with self._transactions() as tx:
            policy = await self.current(tx, version)
            if policy.email_verification:
                address = address or mailbox(login)
                if await tx.administrators.login_exists(login):
                    raise IdentityError("login_in_use", "Login is already in use", 409)
                job = await self.queue(
                    tx, "register", address, login=login, password_hash=password_hash
                )
                purpose = "register"
                result = VerificationPending()
            else:
                result, user_id = await self.finish_registration(tx, login, password_hash, None)
                purpose = "bind"
                job = (
                    await self.queue(tx, purpose, address, user_id=user_id)
                    if address and self.providers.mail_ready
                    else None
                )
        if job:
            self.log_queued(job, purpose)
        return result

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
            await self.current(tx, version)
            row = await tx.email.consume(digest, "register")
            if not row:
                raise unauthorized()
            result, _ = await self.finish_registration(
                tx, row["login"], row["password_hash"], row["email"]
            )

            return result

    async def request_binding(self, session, body, peer):
        address = mailbox(body.email)
        await self._throttle("bind:" + address, peer)
        version = await self.guard("bind_email", body.turnstile_token)
        async with self._access.authenticated(session) as (tx, actor):
            credentials = await tx.directory.credentials(actor.user.login)
        password_hash = credentials[1]
        if not await self._verify(password_hash, body.password.get_secret_value()):
            raise unauthorized()
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
        await self._throttle("forgot:" + address, peer)
        version = await self.guard("forgot_password", body.turnstile_token)
        self.require_mail()
        async with self._transactions() as tx:
            await self.current(tx, version)
            user_id = await tx.email.recipient(address)
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
            await self.current(tx, version)
            user = await tx.directory.user(proof["user_id"], lock=True)
            row = await tx.email.consume(digest, "reset")
            if (
                not row
                or not user
                or user.status != "active"
                or (await tx.email.verified(user.id) != row["email"])
            ):
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
