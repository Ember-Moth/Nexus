from sqlalchemy import text

from ..application.schemas import AuthPolicyView


class EmailRepository:
    def __init__(self, session):
        self.session = session

    async def policy(self, *, share=False):
        lock = " FOR SHARE" if share else ""
        row = (
            (
                await self.session.execute(
                    text(
                        "SELECT email_verification,turnstile,version FROM identity.auth_policy "
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
            UPDATE identity.auth_policy SET email_verification=:email_verification,
                turnstile=:turnstile,version=version+1 WHERE singleton AND version=:version
            RETURNING email_verification,turnstile,version
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

    async def recipient(self, email):
        return (
            await self.session.execute(
                text(
                    "SELECT id FROM identity.users WHERE verified_email=:email "
                    "AND status='active' FOR UPDATE"
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
                digest,expires_at,key_version,ciphertext)
            VALUES(:id,:user_id,:purpose,:email,:login,:password_hash,:digest,
                clock_timestamp()+interval '15 minutes',:version,:ciphertext)
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
        removed = await self.session.execute(
            text("""
            DELETE FROM identity.email_challenges WHERE id IN
            (SELECT id FROM identity.email_challenges WHERE expires_at<=clock_timestamp()
             ORDER BY expires_at LIMIT 100 FOR UPDATE SKIP LOCKED)
            RETURNING id,trace_id,purpose,attempts,(ciphertext IS NOT NULL) AS pending
        """)
        )
        job = (
            (
                await self.session.execute(
                    text("""
            WITH job AS (SELECT id FROM identity.email_challenges WHERE ciphertext IS NOT NULL
                AND expires_at>clock_timestamp() AND next_attempt<=clock_timestamp()
                ORDER BY next_attempt,id LIMIT 1 FOR UPDATE SKIP LOCKED)
            UPDATE identity.email_challenges c SET lease_id=:lease, attempts=attempts+1,
                next_attempt=clock_timestamp()+interval '90 seconds' FROM job WHERE c.id=job.id
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
