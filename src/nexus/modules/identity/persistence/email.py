from sqlalchemy import text

from ..application.schemas import AuthPolicyView

# Matches the expression of email_purge_idx.
PURGE_AT = "(CASE WHEN ciphertext IS NULL THEN expires_at ELSE deliver_by END)"


class EmailRepository:
    def __init__(self, session):
        self.session = session

    async def policy(self, *, share=False):
        lock = " FOR SHARE" if share else ""
        row = (
            (
                await self.session.execute(
                    text(
                        "SELECT registration,email_verification,turnstile,version "
                        "FROM identity.auth_policy "
                        "WHERE singleton" + lock
                    )
                )
            )
            .mappings()
            .one()
        )
        return AuthPolicyView(**row)

    async def set_policy(self, body, version):
        row = (
            (
                await self.session.execute(
                    text("""
            UPDATE identity.auth_policy SET registration=:registration,
                email_verification=:email_verification,turnstile=:turnstile,version=version+1
            WHERE singleton AND version=:version
            RETURNING registration,email_verification,turnstile,version
        """),
                    {**body.model_dump(), "version": version},
                )
            )
            .mappings()
            .one_or_none()
        )
        return AuthPolicyView(**row) if row else None

    async def verified(self, user_id):
        return (
            await self.session.execute(
                text("SELECT verified_email FROM identity.users WHERE id=:id"), {"id": user_id}
            )
        ).scalar_one()

    async def recipient(self, email, *, platform_recovery):
        # Holders of a platform role are recovered by another administrator unless the
        # deployment allows email recovery for them.
        exclude = (
            ""
            if platform_recovery
            else "AND NOT EXISTS (SELECT FROM identity.platform_role_bindings b "
            "WHERE b.user_id=u.id) "
        )
        return (
            await self.session.execute(
                text(
                    "SELECT u.id FROM identity.users u WHERE u.verified_email=:email "
                    "AND u.status='active' " + exclude + "FOR UPDATE OF u"
                ),
                {"email": email},
            )
        ).scalar_one_or_none()

    async def bind(self, user_id, email):
        await self.session.execute(
            text("UPDATE identity.users SET verified_email=:email WHERE id=:id"),
            {"id": user_id, "email": email},
        )

    async def clear(self, user_id, purposes):
        await self.session.execute(
            text("DELETE FROM identity.email_challenges WHERE user_id=:id AND purpose=ANY(:kinds)"),
            {"id": user_id, "kinds": purposes},
        )

    async def queue(self, values):
        await self.session.execute(
            text("""
            INSERT INTO identity.email_challenges(id,user_id,purpose,email,login,password_hash,
                digest,expires_at,deliver_by,key_version,ciphertext)
            VALUES(:id,:user_id,:purpose,:email,:login,:password_hash,:digest,
                clock_timestamp()+interval '15 minutes',
                clock_timestamp()+make_interval(secs => :queue_ttl),:version,:ciphertext)
        """),
            values,
        )

    async def find(self, digest, purpose):
        return (
            (
                await self.session.execute(
                    text("""
            SELECT id,user_id,password_hash FROM identity.email_challenges
            WHERE digest=:digest AND purpose=:purpose AND expires_at>clock_timestamp()
        """),
                    {"digest": digest, "purpose": purpose},
                )
            )
            .mappings()
            .one_or_none()
        )

    async def consume(self, digest, purpose):
        return (
            (
                await self.session.execute(
                    text("""
            DELETE FROM identity.email_challenges
            WHERE digest=:digest AND purpose=:purpose AND expires_at>clock_timestamp()
            RETURNING user_id,email,login,password_hash
        """),
                    {"digest": digest, "purpose": purpose},
                )
            )
            .mappings()
            .one_or_none()
        )

    async def claim(self, lease):
        # A sent challenge lives until its token expires; an unsent one until its delivery
        # deadline, however long ago it was queued.
        removed = await self.session.execute(
            text(f"""
            DELETE FROM identity.email_challenges WHERE id IN
            (SELECT id FROM identity.email_challenges WHERE {PURGE_AT}<=clock_timestamp()
             ORDER BY {PURGE_AT} LIMIT 100 FOR UPDATE SKIP LOCKED)
            RETURNING id,trace_id,purpose,attempts,(ciphertext IS NOT NULL) AS pending
        """)
        )
        # The token is valid for 15 minutes from each delivery attempt, so a queue backlog cannot
        # expire a message before the recipient has seen it.
        job = (
            (
                await self.session.execute(
                    text("""
            WITH job AS (SELECT id FROM identity.email_challenges WHERE ciphertext IS NOT NULL
                AND deliver_by>clock_timestamp() AND next_attempt<=clock_timestamp()
                ORDER BY next_attempt,id LIMIT 1 FOR UPDATE SKIP LOCKED)
            UPDATE identity.email_challenges c SET lease_id=:lease, attempts=attempts+1,
                next_attempt=clock_timestamp()+interval '90 seconds',
                expires_at=clock_timestamp()+interval '15 minutes' FROM job WHERE c.id=job.id
            RETURNING c.id,c.trace_id,c.attempts,c.user_id,c.purpose,c.email,
                      c.key_version,c.ciphertext
        """),
                    {"lease": lease},
                )
            )
            .mappings()
            .one_or_none()
        )
        return job, [row for row in removed.mappings() if row["pending"]]

    async def finish(self, job, lease, sent):
        result = await self.session.execute(
            text("""
            UPDATE identity.email_challenges SET lease_id=NULL,
            ciphertext=CASE WHEN :sent THEN NULL ELSE ciphertext END,
            next_attempt=clock_timestamp()+interval '5 seconds' * (1 << least(attempts,6))
            WHERE id=:id AND lease_id=:lease RETURNING next_attempt
        """),
            {"id": job, "lease": lease, "sent": sent},
        )
        return result.scalar_one_or_none()

    async def reject(self, job, lease):
        result = await self.session.execute(
            text("DELETE FROM identity.email_challenges WHERE id=:id AND lease_id=:lease"),
            {"id": job, "lease": lease},
        )
        return result.rowcount == 1
