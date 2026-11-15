"""Wire resources and implemented use cases; never passed to business services."""

import asyncio
import base64

from nexus.config import RuntimeSettings
from nexus.infrastructure.auth_limits import RedisAuthLimits, RedisMailLimits
from nexus.infrastructure.cache import Cache
from nexus.infrastructure.credential_cipher import CredentialCipher
from nexus.infrastructure.payment_http import PaymentHttp, ProviderHttp
from nexus.infrastructure.postgres import Database
from nexus.modules import billing, catalog, identity, publication, reporting
from nexus.shared.storage import ModuleName


class CatalogAccess:
    def __init__(self, managers, policies):
        self._managers = managers
        self._policies = policies

    async def administrator(self, token):
        return await self._managers.require_platform_admin(token)

    async def visibility(self, token, organization_id, project_id, key_id):
        scope = await self._policies.visibility(token, organization_id, project_id, key_id)
        return catalog.Visibility(scope.actor_id, scope.allowed_models)


class BillingAccess:
    def __init__(self, managers, policies):
        self._managers, self._policies = managers, policies

    async def administrator(self, token):
        return await self._managers.require_platform_admin(token)

    async def authorize(self, token, org_id=None, **kwargs):
        result = await self._managers.financial_access(token, org_id, **kwargs)
        if org_id is None:
            return result
        return billing.Scope(
            result.actor_id,
            result.organization_id,
            result.organization_status,
            result.project_id,
            result.key_id,
        )

    async def visible_models(self, token, org_id, project_id=None, key_id=None):
        return (await self._policies.visibility(token, org_id, project_id, key_id)).allowed_models


class BillingCatalog:
    def __init__(self, lookup):
        self._lookup = lookup

    async def model(self, model_id):
        model = await self._lookup("model", model_id)
        return (
            billing.ModelInfo(
                model.id,
                model.status,
                model.configured,
                frozenset(model.token_dimensions + model.tool_dimensions),
                model.max_input_tokens,
                model.max_output_tokens,
            )
            if model
            else None
        )

    async def deployment(self, deployment_id):
        deployment = await self._lookup("deployment", deployment_id)
        return (
            billing.DeploymentInfo(deployment.id, deployment.model_id, deployment.configured)
            if deployment
            else None
        )


class ResourceAccess:
    """Identity's organization role checks for upstream resources held by API keys."""

    def __init__(self, projects):
        self._projects = projects

    async def scope(
        self, token, organization_id, *, project_id=None, api_key_id=None, manage=False
    ):
        actor, keys = await self._projects.resource_scope(
            token, organization_id, project_id=project_id, key_id=api_key_id, manage=manage
        )
        return publication.ResourceScope(
            actor,
            None if keys is None else {key: publication.KeyOwner(o, p) for key, o, p in keys},
        )

    async def owners(self, key_ids):
        rows = await self._projects.key_owners(key_ids)
        return {key: publication.KeyOwner(o, p) for key, o, p in rows}


class Application:
    def __init__(self, settings: RuntimeSettings) -> None:
        self._timeout = settings.dependency_timeout
        self.database = Database(
            settings.database_url.get_secret_value(),
            runtime_role=f"nexus_{settings.process_role}",
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            timeout=settings.dependency_timeout,
        )
        self.cache = Cache(
            settings.redis_url.get_secret_value(),
            timeout=settings.dependency_timeout,
            namespace=settings.redis_namespace,
        )
        self._storage = {name.value: self.database.for_module(name) for name in ModuleName}
        roots = {
            version: base64.urlsafe_b64decode(secret.get_secret_value())
            for version, secret in settings.previous_encryption_keys.items()
        }
        roots[settings.encryption_key_version] = base64.urlsafe_b64decode(
            settings.encryption_key.get_secret_value()
        )
        self.identity = identity.create_service(
            self._storage[ModuleName.IDENTITY.value],
            RedisAuthLimits(
                self.cache, base64.urlsafe_b64decode(settings.encryption_key.get_secret_value())
            ),
            settings=settings,
            mail_cipher=CredentialCipher(
                settings.encryption_key_version, roots, purpose="identity-mail"
            ),
            mail_limits=RedisMailLimits(
                self.cache, base64.urlsafe_b64decode(settings.encryption_key.get_secret_value())
            ),
        )
        self.projects = identity.create_project_service(self._storage[ModuleName.IDENTITY.value])
        self.model_access = identity.create_model_access_service(
            self._storage[ModuleName.IDENTITY.value],
            catalog.create_model_lookup(self._storage[ModuleName.CATALOG.value]),
        )
        self.catalog = catalog.create_service(
            self._storage[ModuleName.CATALOG.value],
            CatalogAccess(self.identity, self.model_access),
            CredentialCipher(settings.encryption_key_version, roots),
        )

        self.billing_settings = billing.create_settings(
            self._storage[ModuleName.BILLING.value],
            BillingAccess(self.identity, self.model_access),
        )
        self.billing, self.pricing, self.budgets, self.reservations = billing.create_services(
            self._storage[ModuleName.BILLING.value],
            BillingAccess(self.identity, self.model_access),
            BillingCatalog(
                catalog.create_pricing_lookup(
                    self._storage[ModuleName.CATALOG.value], frozenset(roots)
                )
            ),
        )

        self.operations = billing.create_operations(
            self._storage["billing"],
            BillingAccess(self.identity, self.model_access),
            self.reservations,
        )
        self.reports = reporting.create_service(
            self._storage["reporting"],
            self.identity.report_scope,
            billing.create_reports(self._storage["billing"]),
            [
                module.create_audit_reader(self._storage[name])
                for name, module in [
                    ("identity", identity),
                    ("catalog", catalog),
                    ("billing", billing),
                    ("publication", publication),
                ]
            ],
            self.identity.require_platform_admin,
        )
        self.routing = catalog.RoutingPolicies(
            self._storage["catalog"],
            self.identity.financial_access,
            self.identity.require_platform_admin,
        )
        self.publication = publication.create_service(
            self._storage["publication"],
            self.identity.require_platform_admin,
            identity.create_identity_source(self._storage["identity"]),
            catalog.create_catalog_source(
                self._storage["catalog"], CredentialCipher(settings.encryption_key_version, roots)
            ),
            billing.create_price_source(self._storage["billing"]),
            self.cache,
            identity.create_organization_source(self._storage["identity"]),
        )
        self.upstream_resources = publication.create_resource_service(
            self._storage["publication"], ResourceAccess(self.projects)
        )
        self._notifications = self.database.notifications(publication.SOURCE_CHANNEL)
        self.auto_publisher = publication.create_auto_publisher(
            self._storage["publication"],
            self.publication,
            self._notifications,
            poll=settings.auto_publish_poll_seconds,
        )
        self.usage = billing.create_usage_processor(self._storage["billing"], self.reservations)
        self.payment_http = (
            PaymentHttp(settings.mock_payment_url) if settings.mock_payment_url else None
        )
        self.payments, self.payment_processor = billing.create_payment_services(
            self._storage[ModuleName.BILLING.value],
            BillingAccess(self.identity, self.model_access),
            CredentialCipher(settings.encryption_key_version, roots, purpose="payment"),
            self.payment_http,
            RedisAuthLimits(
                self.cache, base64.urlsafe_b64decode(settings.encryption_key.get_secret_value())
            ),
            policy=self.billing_settings.cached,
            environment=settings.environment,
            provider_transport=ProviderHttp(),
            public_base_url=settings.payment_public_base_url,
            return_url=settings.payment_return_url,
        )

    async def metrics(self, token):
        await self.identity.require_platform_admin(token)
        process, billing_metrics, publication_metrics = await asyncio.gather(
            self.database.operational_metrics(),
            self.reports.source.metrics(),
            self.publication.metrics(),
        )
        return {"process": process, "billing": billing_metrics, "publication": publication_metrics}

    async def process_billing(self):
        await self.database.check_revision()
        count = 0
        for event in await self.identity.pending_organizations():
            await self.billing.provision_account(event)
            count += 1
        for event in await self.billing.pending_accounts():
            await self.identity.account_ready(
                event.event_id,
                event.organization_id,
                event.account_id,
                schema_version=event.schema_version,
            )
            await self.billing.acknowledge_account(event.event_id)
            count += 1
        return (
            count + await self.reservations.mark_expired() + await self.payment_processor.process()
        )

    async def readiness(self) -> dict[str, str]:
        checks = {
            "postgres": self.database.check_revision,
            "redis": self.cache.check,
            **{name: scope.check_access for name, scope in self._storage.items()},
        }

        async def probe(name, check) -> tuple[str, str]:
            try:
                async with asyncio.timeout(self._timeout):
                    await check()
            except Exception:
                return name, "unavailable"
            return name, "ok"

        return dict(await asyncio.gather(*(probe(name, check) for name, check in checks.items())))

    async def close(self) -> None:
        await self._notifications.close()
        jobs = [self.database.close(), self.cache.close()]
        if self.payment_http:
            jobs.append(self.payment_http.close())
        await asyncio.gather(*jobs)
