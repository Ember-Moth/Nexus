from decimal import Decimal, localcontext

from nexus.shared.errors import ServiceError

from .common import found, replay, versioned, view
from .schemas import DiscountView, Page, PriceView
from .settings import stored_currency


class PricingService:
    def __init__(self, transactions, access, catalog):
        self._transactions, self._access, self._catalog = transactions, access, catalog

    async def _currency(self, currency):
        if currency != await stored_currency(self._transactions):
            raise ServiceError(
                "currency_mismatch", "Currency must match the deployment billing currency", 422
            )

    async def _load_target(self, kind, scope_id):
        deployment = None
        if kind == "cost":
            deployment = await self._catalog.deployment(scope_id)
            if deployment is None:
                return None, None
            model_id = deployment.model_id
        else:
            model_id = scope_id
        return await self._catalog.model(model_id), deployment

    @staticmethod
    def _validate_target(target, rates, enabled):
        model, deployment = target
        model = found(model)
        if set(rates) != model.dimensions:
            raise ServiceError(
                "price_dimensions_mismatch",
                "Price dimensions must exactly match the public model",
                422,
            )
        if enabled and (model.status != "active" or (deployment and not deployment.configured)):
            raise ServiceError(
                "price_target_inactive", "Enabled pricing requires an active target", 409
            )
        return model.id

    async def list_prices(self, token, kind, limit, cursor, scope_id=None):
        await self._access.authorize(token, operation="pricing")
        async with self._transactions() as repo:
            filters = {"kind": kind}
            if scope_id is not None:
                filters["scope_id"] = scope_id
            rows, cursor = await repo.page("price", None, limit, cursor, **filters)
            return Page[PriceView](items=[view(PriceView, row) for row in rows], next_cursor=cursor)

    async def get_price(self, token, kind, resource_id):
        await self._access.authorize(token, operation="pricing")
        async with self._transactions() as repo:
            row = found(await repo.get("price", resource_id))
            if row.kind != kind:
                found(None)
            return view(PriceView, row)

    async def create_price(self, token, kind, body, key):
        actor = await self._access.authorize(token, operation="pricing")
        await self._currency(body.currency)
        scope_id = body.model_id if kind == "sale" else body.deployment_id
        target = await self._load_target(kind, scope_id)
        async with self._transactions() as repo:
            action = f"price:{kind}:create"
            old, kd, rd = await replay(repo, actor, action, key, body.model_dump(mode="json"))
            if old:
                return view(PriceView, found(await repo.get("price", old.resource_id)))
            model_id = self._validate_target(target, body.rates, False)
            row = await repo.create(
                "price",
                {
                    "kind": kind,
                    "scope_id": scope_id,
                    "model_id": model_id,
                    "currency": body.currency,
                    "rates": {
                        name: rate.model_dump(mode="json") for name, rate in body.rates.items()
                    },
                    "enabled": body.enabled,
                    "created_by": actor,
                },
            )
            await repo.remember(actor, action, kd, rd, "price", row.id)
            await repo.audit(
                actor, "price.created", row.id, details={"kind": kind, "scope_id": str(scope_id)}
            )
            return view(PriceView, row)

    async def edit_price(self, token, kind, resource_id, body, version):
        actor = await self._access.authorize(token, operation="pricing")
        await self._currency(body.currency)
        async with self._transactions() as repo:
            scope_id = found(await repo.get("price", resource_id)).scope_id
        target = await self._load_target(kind, scope_id)
        async with self._transactions() as repo:
            row = found(await repo.get("price", resource_id, lock=True))
            if row.kind != kind:
                found(None)
            versioned(row, version)
            if row.state != "draft":
                raise ServiceError("version_immutable", "Published pricing is immutable", 409)
            self._validate_target(target, body.rates, False)
            await repo.edit(row, body.model_dump(mode="json"))
            await repo.audit(actor, "price.edited", row.id)
            return view(PriceView, row)

    async def publish_price(self, token, kind, resource_id, body, version, key):
        actor = await self._access.authorize(token, operation="pricing")
        async with self._transactions() as repo:
            scope_id = found(await repo.get("price", resource_id)).scope_id
        target = await self._load_target(kind, scope_id)
        async with self._transactions() as repo:
            action = f"price:{kind}:publish"
            old, kd, rd = await replay(
                repo,
                actor,
                action,
                key,
                {"id": str(resource_id), "version": version, **body.model_dump(mode="json")},
            )
            if old:
                return view(PriceView, found(await repo.get("price", old.resource_id)))
            row = found(await repo.get("price", resource_id, lock=True))
            if row.kind != kind:
                found(None)
            versioned(row, version)
            if row.state != "draft":
                raise ServiceError("version_immutable", "Published pricing is immutable", 409)
            await repo.lock(f"price:{kind}:{row.scope_id}")
            self._validate_target(target, row.rates, row.enabled)
            now = await repo.now()
            effective = body.effective_at or now
            last = await repo.last_effective(kind, row.scope_id)
            if effective < now or (last is not None and effective <= last):
                raise ServiceError(
                    "invalid_effective_at",
                    "Publish starts must be non-retroactive and strictly increasing",
                    422,
                )
            await repo.edit(
                row, {"state": "published", "effective_at": effective, "published_at": now}
            )
            await repo.remember(actor, action, kd, rd, "price", row.id)
            await repo.audit(
                actor, "price.published", row.id, details={"effective_at": effective.isoformat()}
            )
            return view(PriceView, row)

    async def list_discounts(self, token, org_id, limit, cursor):
        await self._access.authorize(token, org_id)
        async with self._transactions() as repo:
            rows, cursor = await repo.page("discount", org_id, limit, cursor)
            return Page[DiscountView](
                items=[view(DiscountView, row) for row in rows], next_cursor=cursor
            )

    async def create_discount(self, token, org_id, body, key):
        scope = await self._access.authorize(token, org_id, operation="pricing")
        async with self._transactions() as repo:
            action = f"{org_id}:discount:create"
            old, kd, rd = await replay(
                repo, scope.actor_id, action, key, body.model_dump(mode="json")
            )
            if old:
                return view(
                    DiscountView, found(await repo.get("discount", old.resource_id, org_id))
                )
            row = await repo.create(
                "discount",
                {"organization_id": org_id, "factor": body.factor, "created_by": scope.actor_id},
            )
            await repo.remember(scope.actor_id, action, kd, rd, "discount", row.id)
            await repo.audit(scope.actor_id, "discount.created", row.id, org_id)
            return view(DiscountView, row)

    async def edit_discount(self, token, org_id, resource_id, body, version):
        scope = await self._access.authorize(token, org_id, operation="pricing")
        async with self._transactions() as repo:
            row = found(await repo.get("discount", resource_id, org_id, lock=True))
            versioned(row, version)
            if row.state != "draft":
                raise ServiceError("version_immutable", "Published discount is immutable", 409)
            await repo.edit(row, {"factor": body.factor})
            await repo.audit(scope.actor_id, "discount.edited", row.id, org_id)
            return view(DiscountView, row)

    async def publish_discount(self, token, org_id, resource_id, body, version, key):
        scope = await self._access.authorize(token, org_id, operation="pricing")
        async with self._transactions() as repo:
            action = f"{org_id}:discount:publish"
            old, kd, rd = await replay(
                repo,
                scope.actor_id,
                action,
                key,
                {"id": str(resource_id), "version": version, **body.model_dump(mode="json")},
            )
            if old:
                return view(
                    DiscountView, found(await repo.get("discount", old.resource_id, org_id))
                )
            await repo.lock("discount:" + str(org_id))
            row = found(await repo.get("discount", resource_id, org_id, lock=True))
            versioned(row, version)
            if row.state != "draft":
                raise ServiceError("version_immutable", "Published discount is immutable", 409)
            now = await repo.now()
            effective = body.effective_at or now
            last = await repo.last_effective("discount", org_id)
            if effective < now or (last is not None and effective <= last):
                raise ServiceError(
                    "invalid_effective_at",
                    "Publish starts must be non-retroactive and strictly increasing",
                    422,
                )
            await repo.edit(
                row, {"state": "published", "effective_at": effective, "published_at": now}
            )
            await repo.remember(scope.actor_id, action, kd, rd, "discount", row.id)
            await repo.audit(
                scope.actor_id,
                "discount.published",
                row.id,
                org_id,
                {"factor": str(row.factor), "effective_at": effective.isoformat()},
            )
            return view(DiscountView, row)

    async def effective_pricing(self, token, org_id, model_id, project_id=None, key_id=None):
        allowed = await self._access.visible_models(token, org_id, project_id, key_id)
        if allowed is not None and model_id not in allowed:
            found(None)
        model = found(await self._catalog.model(model_id))
        if model.status != "active" or not model.configured:
            found(None)
        async with self._transactions() as repo:
            now = await repo.now()
            price = await repo.current_price("sale", model_id, now)
            if price is None or not price.enabled or set(price.rates) != model.dimensions:
                raise ServiceError(
                    "price_unavailable", "No complete active sale price is available", 409
                )
            discount = await repo.current_discount(org_id, now)
            with localcontext() as context:
                context.prec = 60
                rates = {
                    name: {
                        **rate,
                        "discounted_unit_price": format(
                            discount.factor * Decimal(rate["unit_price"]), "f"
                        ),
                    }
                    for name, rate in price.rates.items()
                }
            return {
                "model_id": model_id,
                "currency": price.currency,
                "sale_price_version_id": price.id,
                "discount_version_id": discount.id,
                "sale_effective_at": price.effective_at,
                "discount_effective_at": discount.effective_at,
                "discount_factor": format(discount.factor, ".6f"),
                "rates": rates,
                "as_of": now,
            }
