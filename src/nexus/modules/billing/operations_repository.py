import json

from sqlalchemy import text

from nexus.shared.reporting import next_cursor

from .payment_repository import PaymentRepository


class OperationsRepository(PaymentRepository):
    async def execute(self, query, **params):
        return await self._session.execute(text(query), params)

    async def rows(self, query, **params):
        return (await self.execute(query, **params)).mappings().all()

    async def cases(self, org, state, limit, cursor, case_id=None):
        conditions = ["true"]
        values = {"limit": limit + 1}
        for col, value in (("organization_id", org), ("state", state), ("id", case_id)):
            if value is not None:
                conditions.append(f"{col}=:{col}")
                values[col] = value
        if cursor:
            conditions.append("(created_at,id)>(:at,:rid)")
            values.update(at=cursor[0], rid=cursor[1])
        rows = await self.rows(
            """
         WITH cases AS (SELECT id,organization_id,source_type,source_id,request_id,reason,state,
            version, evidence, created_at, resolved_at, resolution_id, NULL::numeric
            amount_at_risk FROM billing.review_cases
         UNION ALL SELECT id,organization_id,'payment',order_id,NULL,reason,status,version,evidence,
            created_at,NULL,decision_id,amount_at_risk FROM billing.payment_cases)
         SELECT * FROM cases WHERE """
            + " AND ".join(conditions)
            + " ORDER BY created_at,id LIMIT :limit",
            **values,
        )
        return {
            "items": [
                {
                    **r,
                    "amount_at_risk": format(r["amount_at_risk"], ".12f")
                    if r["amount_at_risk"] is not None
                    else None,
                }
                for r in rows[:limit]
            ],
            "next_cursor": next_cursor(rows[limit - 1]["created_at"], rows[limit - 1]["id"])
            if len(rows) > limit
            else None,
        }

    async def check_account(self):
        rows = await self.rows("""
                SELECT a.* FROM billing.accounts a LEFT JOIN billing.account_checks c
         ON c.organization_id=a.organization_id WHERE c.checked_at IS NULL OR c.checked_at
         <clock_timestamp()-interval '1 hour'
         ORDER BY c.checked_at NULLS FIRST,a.id LIMIT 1 FOR UPDATE OF a SKIP LOCKED""")
        if not rows:
            return 0
        org = rows[0]["organization_id"]
        account = await self.account(org)
        issues = await self.reconcile(account)
        await self.execute(
            """INSERT INTO billing.account_checks VALUES(:org,clock_timestamp(),:n)
            ON CONFLICT(organization_id) DO UPDATE SET checked_at=excluded.checked_at,
            differences=excluded.differences""",
            org=org,
            n=len(issues),
        )
        if issues:
            await self.execute(
                """
                INSERT INTO billing.review_cases(organization_id,source_type,source_id,
              reason,evidence)
              VALUES(:org,'account',:acct,'reconciliation_difference',cast(:facts AS jsonb))
              ON CONFLICT(source_type, source_id) DO UPDATE SET state='open', resolved_at=NULL,
              resolution_id=NULL,
              evidence=excluded.evidence,version=billing.review_cases.version+1""",
                org=org,
                acct=account.id,
                facts=json.dumps(issues, default=str),
            )
        return 1

    async def decision(self, id):
        rows = await self.rows("SELECT * FROM billing.case_decisions WHERE id=:id", id=id)
        return dict(rows[0]) if rows else None

    async def decide(self, id, case, actor, body, result):
        await self.execute(
            """INSERT INTO billing.case_decisions(id,case_id,organization_id,actor_id,
          action,reason,evidence,parameters,result) VALUES(:id,:cid,:org,:actor,:action,:reason,
          :evidence,cast(:params AS jsonb),cast(:result AS jsonb))""",
            id=id,
            cid=case["id"],
            org=case["organization_id"],
            actor=actor,
            action=body.action,
            reason=body.reason,
            evidence=body.evidence,
            params=body.model_dump_json(),
            result=json.dumps(result, default=str),
        )

    async def close_case(self, case, decision_id):
        await self.execute(
            """UPDATE billing.review_cases SET state='resolved',resolved_at=clock_timestamp(),
           resolution_id=:decision,version=version+1 WHERE id=:id""",
            id=case["id"],
            decision=decision_id,
        )

    async def clear_restriction(self, account, kind, id):
        await self.execute(
            """UPDATE billing.account_restrictions SET released_at=clock_timestamp()
          WHERE account_id=:acct AND source_type=:kind AND source_id=:id AND released_at IS NULL""",
            acct=account.id,
            kind=kind,
            id=id,
        )

    async def unresolved(self, account):
        return (
            await self.execute(
                """SELECT count(*) FROM billing.account_restrictions
            WHERE account_id=:acct AND released_at IS NULL""",
                acct=account.id,
            )
        ).scalar_one()
