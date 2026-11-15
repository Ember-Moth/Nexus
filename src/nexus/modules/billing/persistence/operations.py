import json

from sqlalchemy import text

from nexus.shared.reporting import next_cursor

from .payments import PaymentRepository


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

    async def case_detail(self, case):
        decisions = await self.rows(
            """SELECT id,actor_id,action,reason,evidence,parameters,result,created_at
            FROM billing.case_decisions WHERE case_id=:id ORDER BY created_at,id""",
            id=case["id"],
        )
        data = {**case, "decisions": [dict(r) for r in decisions]}
        if case["request_id"]:
            data["request"] = (
                await self.rows(
                    "SELECT payload FROM billing.report_source WHERE id=:id",
                    id=case["request_id"],
                )
            )[0]["payload"]
            data["usage_evidence"] = [
                dict(r)
                for r in await self.rows(
                    """SELECT e.id,e.envelope,e.received_at,
             n.rule_version,n.quantities,n.review_reason FROM billing.usage_events e
             LEFT JOIN billing.normalized_usage n ON n.event_id=e.id WHERE e.request_id=:rid
             ORDER BY e.received_at, e.id""",
                    rid=case["request_id"],
                )
            ]
        return data

    async def latest_cost(self, attempt):
        prior = await self.rows(
            "SELECT amount FROM billing.cost_corrections WHERE attempt_id=:id "
            "ORDER BY created_at DESC,id DESC LIMIT 1",
            id=attempt.id,
        )
        return prior[0]["amount"] if prior else attempt.cost_amount

    async def insert_statement(self, reference, body, attempt_id, compared, actor_id):
        await self.execute(
            """INSERT INTO billing.upstream_statements(id,organization_id,attempt_id,
         external_reference,currency,amount,compared_cost,evidence,actor_id)
         VALUES(:id,:org,:aid,:ref,:currency,:amount,:cost,:evidence,:actor)""",
            id=reference,
            org=body.organization_id,
            aid=attempt_id,
            ref=body.external_reference,
            currency=body.currency,
            amount=body.amount,
            cost=compared,
            evidence=body.evidence,
            actor=actor_id,
        )

    async def attach_statement(self, attempt_id, reference):
        await self.execute(
            """UPDATE billing.review_cases SET evidence=jsonb_build_object(
          'statement_id', cast(:statement AS text)), version=version+1 WHERE
          source_type='attempt' AND source_id=:id""",
            statement=str(reference),
            id=attempt_id,
        )

    async def bind_guardrail_receipt(self, attempt, evidence, quantities):
        params = {
            "aid": attempt.id,
            "supplier": attempt.meter_supplier_id,
            "ref": evidence,
            "units": json.dumps(quantities),
        }
        await self.execute(
            """
            INSERT INTO billing.guardrail_usage_receipts
                (attempt_id,supplier_id,reference_digest,quantities)
            VALUES(:aid,:supplier,encode(sha256(convert_to(:ref,'UTF8')),'hex'),
                   CAST(:units AS jsonb))
            ON CONFLICT DO NOTHING""",
            **params,
        )
        return (
            await self.execute(
                """SELECT EXISTS(SELECT FROM billing.guardrail_usage_receipts
            WHERE attempt_id=:aid AND supplier_id=:supplier
              AND reference_digest=encode(sha256(convert_to(:ref,'UTF8')),'hex')
              AND quantities=CAST(:units AS jsonb))""",
                **params,
            )
        ).scalar_one()

    async def bind_execution_receipt(self, request_id, attempt, evidence, seconds):
        # One supplier line can support procurement and sale for the same operation.
        # It cannot be applied to a second request or silently change the duration.
        row = await self._session.execute(
            text("""
            INSERT INTO billing.execution_usage_receipts(request_id,deployment_id,supplier_id,
                reference_digest,confirmed_seconds)
            VALUES(:rid,:dep,:supplier,encode(sha256(convert_to(:ref,'UTF8')),'hex'),:seconds)
            ON CONFLICT DO NOTHING
        """),
            {
                "rid": request_id,
                "dep": attempt.deployment_id,
                "supplier": attempt.meter_supplier_id,
                "ref": evidence,
                "seconds": seconds,
            },
        )
        if row.rowcount == 1:
            return True
        return bool(
            (
                await self._session.execute(
                    text("""
            SELECT EXISTS(SELECT FROM billing.execution_usage_receipts WHERE request_id=:rid
            AND supplier_id=:supplier AND deployment_id=:dep
            AND reference_digest=encode(sha256(convert_to(:ref,'UTF8')),'hex')
            AND confirmed_seconds=:seconds)
        """),
                    {
                        "rid": request_id,
                        "dep": attempt.deployment_id,
                        "supplier": attempt.meter_supplier_id,
                        "ref": evidence,
                        "seconds": seconds,
                    },
                )
            ).scalar_one()
        )

    async def normalized_quantities(self, attempt_id):
        rows = await self.rows(
            "SELECT quantities FROM billing.normalized_usage WHERE attempt_id=:id", id=attempt_id
        )
        return rows[0]["quantities"] if rows else None

    async def wake_existing_usage(self, request_id):
        await self.execute(
            """
                UPDATE billing.usage_jobs SET state='ready',reason=NULL,
              next_attempt_at=clock_timestamp(),
                generation=generation+1 WHERE request_id=:rid""",
            rid=request_id,
        )

    async def statement_amount(self, statement_id, organization_id, attempt_id):
        rows = await self.rows(
            """SELECT amount FROM billing.upstream_statements WHERE id=:id
               AND organization_id=:org AND attempt_id=:aid""",
            id=statement_id,
            org=organization_id,
            aid=attempt_id,
        )
        return rows[0]["amount"] if rows else None

    async def append_cost_correction(
        self, correction, attempt_id, decision, amount, quantities, price_id
    ):
        await self.execute(
            """INSERT INTO billing.cost_corrections(id, attempt_id, decision_id, amount, quantities,
        cost_price_version_id)
                    VALUES(:id,:aid,:decision,:amount,cast(:quantities AS jsonb),:price)""",
            id=correction,
            aid=attempt_id,
            decision=decision,
            amount=amount,
            quantities=json.dumps(quantities),
            price=price_id,
        )

    async def close_payment_case(self, case_id, decision, basis):
        await self.execute(
            """UPDATE billing.payment_cases SET status='resolved',version=version+1,
            amount_at_risk=0, decision_id=:decision, decision_basis=cast(:basis AS jsonb) WHERE
            id=:id""",
            decision=decision,
            basis=json.dumps(basis),
            id=case_id,
        )

    async def open_payment_reasons(self, order_id):
        rows = await self.rows(
            "SELECT reason FROM billing.payment_cases WHERE order_id=:id AND "
            "status='open' ORDER BY created_at,id",
            id=order_id,
        )
        return [row["reason"] for row in rows]

    async def release_cleared_restrictions(self, account_id, org):
        await self.execute(
            """UPDATE billing.account_restrictions f SET released_at=clock_timestamp()
          WHERE account_id=:acct AND released_at IS NULL AND source_type='request'
          AND EXISTS(SELECT FROM billing.request_runs r WHERE r.id=f.source_id
            AND r.account_id=f.account_id AND r.reservation_state<>'held')""",
            acct=account_id,
        )
        await self.execute(
            """UPDATE billing.account_restrictions f SET released_at=clock_timestamp()
          WHERE account_id=:acct AND source_type='legacy' AND released_at IS NULL AND NOT EXISTS
          (SELECT FROM billing.review_cases WHERE organization_id=:org AND state='open') AND
          NOT EXISTS
          (SELECT FROM billing.payment_cases WHERE organization_id=:org AND status='open')""",
            acct=account_id,
            org=org,
        )
