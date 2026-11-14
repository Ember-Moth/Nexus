"""One posting primitive for manual and verified gateway receipts."""

from nexus.shared.errors import ServiceError


async def post_funding_record(repo, account, record, *, actor_id=None, system_actor=None):
    if (
        record.account_id != account.id
        or record.organization_id != account.organization_id
        or record.currency != account.currency
    ):
        raise ServiceError(
            "funding_binding_conflict", "Funding does not belong to this account", 409
        )
    if record.status != "draft":
        raise ServiceError(
            "invalid_funding_state", "Only an unposted funding record can be posted", 409
        )
    if record.source_type == "manual":
        valid = actor_id is not None and system_actor is None and record.payment_order_id is None
    else:
        valid = (
            record.source_type == "gateway"
            and actor_id is None
            and system_actor == "payment-worker"
            and record.system_actor == system_actor
            and record.payment_order_id is not None
            and record.payment_receipt_id is not None
        )
    if not valid:
        raise ServiceError(
            "invalid_funding_actor", "Funding requires its designated execution identity", 403
        )
    entry = await repo.append_entry(
        account, record.amount, "funding_credit", "funding", record.id, actor_id
    )
    await repo.edit(
        record, {"status": "posted", "posted_by": actor_id, "posted_at": await repo.now()}
    )
    details = {
        "amount": str(record.amount),
        "currency": record.currency,
        "source_type": record.source_type,
        "actor_type": "system" if system_actor else "user",
    }
    if system_actor:
        details["system_actor"] = system_actor
    await repo.audit(actor_id, "funding.posted", record.id, record.organization_id, details)
    await repo.funding_posted(record, entry.id)
    return entry
