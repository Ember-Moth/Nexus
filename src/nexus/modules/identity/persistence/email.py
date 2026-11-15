from sqlalchemy import text

from nexus.shared.views import managed_source

from ..application.schemas import AuthPolicyView

# Columns of identity.auth_policy and the policy group each one belongs to.
GROUPS = {
    "sign_in": {
        "window_seconds": "login_window_seconds",
        "per_account": "login_per_account",
        "per_ip": "login_per_ip",
    },
    "mail": {
        "window_seconds": "mail_window_seconds",
        "per_recipient": "mail_per_recipient",
        "per_recipient_total": "mail_per_recipient_total",
        "per_account": "mail_per_account",
        "per_ip": "mail_per_ip",
        "global_limit": "mail_global",
        "queue_ttl_seconds": "mail_queue_ttl_seconds",
    },
    "sessions": {
        "session_ttl_seconds": "session_ttl_seconds",
        "password_setup_ttl_seconds": "password_setup_ttl_seconds",
    },
}
FLAT = (
    "registration",
    "email_verification",
    "turnstile",
    "reserved_logins",
    "platform_email_recovery",
)
WRITABLE = FLAT + tuple(c for group in GROUPS.values() for c in group.values())
POLICY_COLUMNS = ",".join((*WRITABLE, "version", "managed_sha256", "managed_at"))
# Sentinel: an administrator's write leaves the lock as it is (it is refused while set).
KEEP = object()


def policy_view(row):
    values = {name: row[name] for name in (*FLAT, "version")}
    for group, columns in GROUPS.items():
        values[group] = {field: row[column] for field, column in columns.items()}
    return AuthPolicyView(**values, managed=managed_source(row))


def policy_columns(policy):
    values = {name: getattr(policy, name) for name in FLAT}
    for group, columns in GROUPS.items():
        part = getattr(policy, group)
        values.update({column: getattr(part, field) for field, column in columns.items()})
    return values


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
                    text(f"SELECT {POLICY_COLUMNS} FROM identity.auth_policy WHERE singleton{lock}")
                )
            )
            .mappings()
            .one()
        )
        return policy_view(row)

    async def set_policy(self, policy, version, *, managed=KEEP):
        """Store a complete policy if the stored version is still ``version``.

        ``managed`` is the SHA-256 of the policy file that locks the row, or None to unlock it.
        """
        lock = (
            ""
            if managed is KEEP
            else ",managed_sha256=:managed,managed_at=CASE WHEN CAST(:managed AS text) IS NULL "
            "THEN NULL ELSE clock_timestamp() END"
        )
        params = {**policy_columns(policy), "version": version}
        if managed is not KEEP:
            params["managed"] = managed
        row = (
            (
                await self.session.execute(
                    text(f"""
            UPDATE identity.auth_policy SET {", ".join(f"{c}=:{c}" for c in WRITABLE)},
                version=version+1{lock}
            WHERE singleton AND version=:version
            RETURNING {POLICY_COLUMNS}
        """),
                    params,
                )
            )
            .mappings()
            .one_or_none()
        )
        return policy_view(row) if row else None

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
