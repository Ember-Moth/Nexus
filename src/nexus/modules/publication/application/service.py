import base64
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from nexus.shared.errors import ServiceError
from nexus.shared.policy import ImportPlan, changes, plan
from nexus.shared.views import managed_source

from .ports import OrganizationSource
from .schemas import (
    AUTO_PUBLISH_DEFAULTS,
    CAPABILITIES,
    CLUSTER_POLICY_CAPABILITY,
    FEATURE_CAPABILITIES,
    RELAY_POLICY_DEFAULTS,
    ClusterPolicy,
)

# Audit resource of changes to the singleton cluster policy defaults.
POLICY_RESOURCE = UUID(int=0)
# The audit identity of releases made by the auto-publisher. Request handlers always
# resolve a person from a session token, so no HTTP caller can act as this actor.
AUTO_PUBLISHER = "auto_publisher"


def serialize(value):
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str
    ).encode()


def short_digest(value):
    return hashlib.sha256(serialize(value)).hexdigest()[:16]


def scope_digest(cluster):
    """Identify the configured scope; a widened scope changes it without a new generation."""
    return short_digest(
        {
            "scope": cluster["organization_scope"],
            "organizations": sorted(map(str, cluster["organization_ids"])),
            "deployments": sorted(map(str, cluster["deployment_ids"])),
        }
    )


def relay_policy(cluster):
    """The cluster's effective Relay policy, as read with the cluster."""
    return {name: cluster[name] for name in RELAY_POLICY_DEFAULTS}


def auto_publish_timing(cluster):
    """Quiet window and maximum delay of the cluster's automatic releases, in seconds."""
    quiet = float(cluster["auto_publish_quiet_seconds"])
    # An override can meet a later default; the maximum delay never undercuts the quiet window.
    return {
        "quiet_seconds": quiet,
        "max_delay_seconds": max(float(cluster["auto_publish_max_delay_seconds"]), quiet),
    }


def grouped(row):
    """A policy row as {"relay": {...}, "auto_publish": {...}}."""
    return {
        "relay": {name: row[name] for name in RELAY_POLICY_DEFAULTS},
        "auto_publish": {name: row[f"auto_publish_{name}"] for name in AUTO_PUBLISH_DEFAULTS},
    }


def columns(policy) -> dict:
    values = dict(policy["relay"])
    values.update({f"auto_publish_{k}": v for k, v in policy["auto_publish"].items()})
    return values


def effective(defaults, overrides):
    return {
        group: {
            name: defaults[group][name] if value is None else value
            for name, value in overrides[group].items()
        }
        for group in overrides
    }


def source_vector(cluster, identity, catalog, billing, prices):
    return {
        "scope_generation": cluster["scope_generation"],
        "key_version": cluster["key_version"],
        "scope": scope_digest(cluster),
        "identity": identity,
        "catalog": catalog,
        "billing": billing,
        "effective_prices": sorted(v[0] for v in prices.values()),
        "relay_policy": short_digest(relay_policy(cluster)),
    }


def content_digest(scope_generation, key_version, required, resources):
    """What a node would load; releases with equal digests are interchangeable."""
    return hashlib.sha256(
        serialize(
            {
                "scope_generation": scope_generation,
                "key_version": key_version,
                "required_capabilities": required,
                "resources": resources,
            }
        )
    ).hexdigest()


def trigger(previous, current):
    """Source entries that changed, as [previous, current]; lists appear as digests."""

    def shown(value):
        if value is None:
            return None
        return short_digest(value) if isinstance(value, list) else str(value)

    previous = previous or {}
    return {
        name: [shown(previous.get(name)), shown(value)]
        for name, value in current.items()
        if previous.get(name) != value
    }


def required_capabilities(current):
    required = list(CAPABILITIES)
    profiles = [d["relay_profile"] for d in current["deployments"] if "relay_profile" in d]
    if any(p["endpoint"] == "openai_chat_completions" for p in profiles):
        required.append("relay.responses-to-chat.v1")
    if any(p["endpoint"] == "bedrock_converse" for p in profiles):
        required.append("relay.bedrock-converse.v1")
    if any(p.get("bedrock_guardrail") or p.get("bedrock_model_fields") for p in profiles):
        required.append("relay.bedrock-settings.v1")
    if any(
        p.get("bedrock_guardrail")
        or any(o.get("guardrailConfig") for o in p.get("bedrock_request_options", []))
        for p in profiles
    ) or any(
        any(k.startswith("guardrail_") for k in m.get("tool_dimensions", []))
        for m in current["models"]
    ):
        required.append("relay.bedrock-guardrail-usage.v1")
    if any(p.get("bedrock_request_options") for p in profiles):
        required.append("relay.bedrock-request-options.v1")
    if any(p.get("code_execution") for p in profiles) or any(
        "code_execution_seconds" in m.get("tool_dimensions", []) for m in current["models"]
    ):
        required.append("relay.claude-execution.v1")
    if any(p.get("container_reserve_minutes") for p in profiles) or any(
        any(k.startswith("container_") for k in m.get("tool_dimensions", []))
        for m in current["models"]
    ):
        required.append("relay.openai-containers.v1")
    if any(p.get("bedrock_invoke") for p in profiles):
        required.append("relay.bedrock-claude-invoke.v1")
    if any("web_search_requests" in m.get("tool_dimensions", []) for m in current["models"]) or any(
        "web_search" in p.get("provider_features", []) for p in profiles
    ):
        required.append("relay.web-search.v1")
    if any(
        set(m.get("tool_dimensions", [])) & {"file_search_calls", "vector_storage_byte_seconds"}
        for m in current["models"]
    ):
        required.append("relay.file-search.v1")
    if any(p.get("auto_prompt_cache") for p in profiles):
        required.append("relay.prompt-cache.v1")
    # Nodes without the capability would run with their built-in policy instead.
    if "cluster_policy" in current or "resource_limits" in current:
        required.append(CLUSTER_POLICY_CAPABILITY)
    if profiles:
        required.append("relay.native.v1")
        # Nodes must enforce provider feature switches before serving any profile.
        required.append("relay.provider-features.v1")
    enabled = {f for p in profiles for f in p.get("provider_features", [])}
    required.extend(
        capability
        for feature, capability in FEATURE_CAPABILITIES.items()
        if feature in enabled and capability not in required
    )
    if any(p.get("streaming") and "http" in p["transports"] for p in profiles):
        required.append("relay.sse.v1")
    if any("websocket" in p["transports"] for p in profiles):
        required.append("relay.responses-ws.v1")
    for source, capability in [
        ("anthropic_messages", "relay.messages-to-responses.v1"),
        ("openai_responses", "relay.responses-to-messages.v1"),
    ]:
        if any(
            p.get("convert_from") == source and p["endpoint"] != "openai_chat_completions"
            for p in profiles
        ):
            required.append(capability)
    return required


def automation_state(cluster):
    if not cluster["auto_publish"]:
        return "paused" if cluster.get("paused_at") else "disabled"
    if cluster.get("failing"):
        return "failing"
    if cluster.get("pending_since"):
        return "pending"
    return "current" if cluster.get("checked_at") else "unknown"


class ScopeMoved(Exception):
    """An organization changed state between resolving a rule-based scope and reading it."""


@dataclass
class Sources:
    vector: dict
    resources: dict
    envelopes: dict
    organizations: list
    models: list


class PublicationService:
    def __init__(
        self, transactions, administrator, identity, catalog, prices, cache, organizations
    ):
        self.tx = transactions
        self.admin = administrator
        self.identity = identity
        self.catalog = catalog
        self.prices = prices
        self.cache = cache
        self.organizations: OrganizationSource = organizations

    @staticmethod
    def public(cluster):
        def at(value):
            return value.isoformat() if value else None

        status = {
            "state": automation_state(cluster),
            "pending_since": at(cluster.get("pending_since")),
            "checked_at": at(cluster.get("checked_at")),
            "last_published": cluster.get("published_revision")
            and {
                "revision": str(cluster["published_revision"]),
                "at": at(cluster["published_at"]),
                "trigger": cluster["published_trigger"] or {},
            },
            "last_failure": cluster.get("failure_at")
            and {
                "code": cluster["failure_code"],
                "message": cluster["failure_message"],
                "at": at(cluster["failure_at"]),
            },
            "paused": cluster.get("paused_at")
            and {
                "reason": "rollback",
                "revision": str(cluster["paused_revision"]),
                "at": at(cluster["paused_at"]),
            },
        }
        names = (
            "id",
            "name",
            "region",
            "organization_scope",
            "organization_ids",
            "deployment_ids",
            "key_version",
            "active",
            "auto_publish",
            "version",
            "scope_generation",
        )
        return {
            **{name: cluster[name] for name in names},
            "public_key": base64.b64encode(cluster["public_key"]).decode(),
            "desired_revision": str(cluster["desired_revision"]),
            "distributed_revision": str(cluster["distributed_revision"]),
            "auto_publish_status": status,
        }

    async def create(self, token, body):
        actor = await self.admin(token)
        cid = uuid4()
        async with self.tx() as repo:
            await repo.create_cluster(cid, body, base64.b64decode(body.public_key))
            await repo.audit(actor, "cluster.created", cid)
            return self.public(await repo.cluster(cid))

    async def clusters(self, token, limit, cursor):
        await self.admin(token)
        async with self.tx() as repo:
            rows, next_cursor = await repo.list_clusters(limit, cursor)
            return {"items": [self.public(row) for row in rows], "next_cursor": next_cursor}

    @staticmethod
    def _narrows_organizations(c, scope, orgs):
        if scope == "all" or (scope == c["organization_scope"] == "personal"):
            return False
        if scope == c["organization_scope"] == "listed":
            return not set(c["organization_ids"]) <= set(orgs)
        # Moving to a narrower rule or to a fixed list can drop organizations.
        return True

    async def cluster(self, token, cid):
        await self.admin(token)
        async with self.tx() as repo:
            return self.public(await repo.cluster(cid))

    async def principals(self, token, cid, limit, cursor):
        await self.admin(token)
        async with self.tx() as repo:
            await repo.cluster(cid)
            rows, next_cursor = await repo.list_principals(cid, limit, cursor)
        items = [{**row, "applied_revision": str(row["applied_revision"])} for row in rows]
        return {"items": items, "next_cursor": next_cursor}

    async def releases(self, token, cid, limit, cursor):
        await self.admin(token)
        async with self.tx() as repo:
            await repo.cluster(cid)
            rows, next_cursor = await repo.list_releases(cid, limit, cursor)
        items = [
            {
                "revision": str(row["revision"]),
                "sha256": row["sha256"],
                "created_at": row["created_at"],
                "actor_id": row["actor_id"],
                "initiator": "operator" if row["manual"] else AUTO_PUBLISHER,
                "rollback_revision": row["rollback_revision"],
                "distribution": {"attempts": row["attempts"], "delivered_at": row["delivered_at"]},
            }
            for row in rows
        ]
        return {"items": items, "next_cursor": next_cursor}

    async def update(self, token, cid, body, version):
        actor = await self.admin(token)
        async with self.tx() as repo:
            c = await repo.cluster(cid, True)
            if c["version"] != version:
                raise ServiceError("version_conflict", "Cluster version changed", 412)
            scope = body.organization_scope or c["organization_scope"]
            if scope == "listed":
                orgs = body.organization_ids or (
                    c["organization_ids"] if c["organization_scope"] == "listed" else None
                )
                if not orgs:
                    raise ServiceError(
                        "invalid_scope",
                        "A listed organization scope requires organization_ids",
                        422,
                    )
            elif body.organization_ids:
                raise ServiceError(
                    "invalid_scope",
                    "A rule-based organization scope takes no organization_ids",
                    422,
                )
            else:
                orgs = []
            scope_changed = self._narrows_organizations(c, scope, orgs) or (
                body.deployment_ids is not None
                and not set(c["deployment_ids"]) <= set(body.deployment_ids)
            )
            new_key = base64.b64decode(body.public_key) if body.public_key else None
            await repo.update_cluster(c, body, new_key, scope_changed, scope, orgs)
            await repo.audit(actor, "cluster.updated", cid, {"previous_version": version})
            return self.public(await repo.cluster(cid))

    async def cluster_defaults(self, token):
        await self.admin(token)
        async with self.tx() as repo:
            return self._defaults_view(await repo.defaults())

    @staticmethod
    def _defaults_view(row):
        return {**grouped(row), "managed": managed_source(row), "version": row["version"]}

    async def update_cluster_defaults(self, token, body, version):
        actor = await self.admin(token)
        async with self.tx() as repo:
            current = await repo.defaults(True)
            if current["managed_sha256"] is not None:
                raise ServiceError(
                    "policy_managed_externally",
                    "The cluster policy defaults are managed by a policy file",
                    409,
                )
            if current["version"] != version or not await repo.set_defaults(
                columns(body.model_dump()), version
            ):
                raise ServiceError("version_conflict", "Cluster policy defaults changed", 412)
            found = changes(grouped(current), body.model_dump(mode="json"))
            await repo.audit(actor, "cluster_defaults.updated", POLICY_RESOURCE, found)
            return self._defaults_view(await repo.defaults())

    async def plan_import(self, mode, values, file_sha256):
        """Plan the ``publication.cluster_defaults`` group of a policy file."""
        group = "publication.cluster_defaults"
        async with self.tx() as repo:
            current = await repo.defaults()
        target, found, skipped = plan(
            group,
            mode,
            grouped(current),
            current["version"],
            current["managed_sha256"] is not None,
            values,
            ClusterPolicy,
        )

        async def write(details):
            async with self.tx() as repo:
                if not await repo.set_defaults(
                    columns(target),
                    current["version"],
                    managed=file_sha256 if mode == "managed" else None,
                ):
                    raise ServiceError("version_conflict", "Cluster policy defaults changed", 412)
                await repo.audit(
                    None, "cluster_defaults.updated", POLICY_RESOURCE, {**details, **found}
                )

        return ImportPlan(group, mode, mode == "managed", found, skipped, write if found else None)

    @staticmethod
    def _policy_view(defaults, row):
        overrides = grouped(row)
        return {
            "cluster_id": row["cluster_id"],
            "overrides": overrides,
            "effective": effective(grouped(defaults), overrides),
            "version": row["version"],
        }

    async def policy(self, token, cid):
        await self.admin(token)
        async with self.tx() as repo:
            return self._policy_view(await repo.defaults(), await repo.overrides(cid))

    async def update_policy(self, token, cid, body, version):
        """Replace the cluster's overrides; null values follow the platform defaults."""
        actor = await self.admin(token)
        async with self.tx() as repo:
            defaults = await repo.defaults()
            current = await repo.overrides(cid, True)
            proposed = body.model_dump(mode="json")
            try:
                ClusterPolicy.model_validate(effective(grouped(defaults), proposed))
            except ValueError:
                raise ServiceError(
                    "invalid_cluster_policy",
                    "max_delay_seconds must not be shorter than quiet_seconds",
                    422,
                ) from None
            if current["version"] != version or not await repo.set_overrides(
                cid, columns(proposed), version
            ):
                raise ServiceError("version_conflict", "Cluster policy changed", 412)
            await repo.audit(
                actor, "cluster.policy_updated", cid, changes(grouped(current), proposed)
            )
            return self._policy_view(defaults, await repo.overrides(cid))

    async def principal(self, token, cid, body):
        actor = await self.admin(token)
        async with self.tx() as repo:
            await repo.cluster(cid, True)
            await repo.register(cid, body)
            await repo.audit(actor, "principal.configured", body.node_id, {"cluster_id": str(cid)})
            return body

    async def _scope(self, c):
        if c["organization_scope"] == "listed":
            return list(c["organization_ids"])
        personal = c["organization_scope"] == "personal"
        return [o["id"] for o in await self.organizations() if o["personal"] or not personal]

    async def _sources(self, c):
        orgs = await self._scope(c)
        deps = c["deployment_ids"]
        try:
            identity_rev, identity = await self.identity(orgs)
        except ValueError:
            if c["organization_scope"] != "listed":
                raise ScopeMoved from None
            raise
        catalog_rev, catalog, envelopes = await self.catalog(
            orgs, deps, c["public_key"], c["id"], c["key_version"]
        )
        models = [m["id"] for m in catalog["models"]]
        pricing_rev, prices = await self.prices(models, deps)
        for model in catalog["models"]:
            price = prices.get(("sale", model["id"]))
            if not price or price[1] != set(
                model["token_dimensions"] + model.get("tool_dimensions", [])
            ):
                raise ValueError("Missing complete sale price")
        for dep in catalog["deployments"]:
            cost = prices.get(("cost", dep["id"]))
            if not cost or cost[1] != prices["sale", dep["model_id"]][1]:
                raise ValueError("Missing complete cost price")
        # Route lists are explicit; publishing an unroutable deployment is an operator error.
        routed = {d["deployment_id"] for r in catalog["routes"] for d in r["candidates"]}
        if routed != set(map(str, deps)):
            raise ValueError("Every scoped deployment needs an explicit route")
        for policy in identity["access_policies"]:
            policy["allowed_models"] = [m for m in policy["allowed_models"] if m in models]
        policy = relay_policy(c)
        extra = {"cluster_policy": policy} if policy != RELAY_POLICY_DEFAULTS else {}
        return Sources(
            source_vector(c, identity_rev, catalog_rev, pricing_rev, prices),
            json.loads(serialize({**identity, **catalog, **extra})),
            envelopes,
            orgs,
            models,
        )

    async def _read(self, cluster):
        """Read the sources twice with the same result, retrying while they move."""
        for _ in range(3):
            try:
                first = await self._sources(cluster)
                again = await self._sources(cluster)
            except ScopeMoved:
                continue
            except ValueError as error:
                raise ServiceError("invalid_snapshot", str(error), 422) from None
            if first.vector == again.vector and first.resources == again.resources:
                return first
        raise ServiceError("source_changed", "Configuration changed during publication", 409)

    @staticmethod
    def _rollback(sources, old, rollback_revision):
        current = sources.resources
        old_resources = old["resources"]
        for name in (
            "models",
            "deployments",
            "access_policies",
            "key_verifiers",
            "credential_refs",
        ):
            if any(resource not in current[name] for resource in old_resources[name]):
                raise ServiceError(
                    "rollback_target_unavailable", "Historical references have changed", 409
                )
        # A rule-based scope is judged by the organizations it resolves to now.
        scoped = set(map(str, sources.organizations))
        if any(
            r["organization_id"] not in scoped
            for r in old_resources["routes"] + old_resources["quota_policies"]
        ):
            raise ServiceError("rollback_scope_changed", "Historical tenant scope has changed", 409)
        selected = {r["credential_id"] for r in old_resources["credential_refs"]}
        sources.resources = old_resources
        sources.envelopes = {k: v for k, v in sources.envelopes.items() if str(k) in selected}
        sources.vector["rollback_revision"] = str(rollback_revision)

    @staticmethod
    async def _published_digest(repo, locked):
        rev = locked["desired_revision"]
        if not rev:
            return None
        if locked.get("content_revision") == rev and locked.get("content_digest"):
            return locked["content_digest"]
        # Releases stored before the digest was recorded are digested from their bytes.
        snapshot = await repo.snapshot(locked["id"], rev)
        manifest = json.loads(bytes(snapshot["payload_bytes"]))
        return content_digest(
            snapshot["source_revision"]["scope_generation"],
            snapshot["source_revision"]["key_version"],
            manifest["required_capabilities"],
            manifest["resources"],
        )

    async def _commit(
        self, repo, cluster, locked, sources, *, actor, key, params, rollback, previous
    ):
        """Publish while holding the cluster row lock; automatic releases have no actor."""
        cid = cluster["id"]
        automatic = actor is None
        if (
            locked["version"] != cluster["version"]
            or locked["policy_version"] != cluster["policy_version"]
            or locked["defaults_version"] != cluster["defaults_version"]
            or locked["desired_revision"] != cluster["desired_revision"]
            or not locked["active"]
            or (automatic and not locked["auto_publish"])
        ):
            raise ServiceError("cluster_changed", "Cluster changed during publication", 409)
        current = sources.resources
        nodes = await repo.capabilities(cid)
        required = required_capabilities(current)
        if any(not set(required) <= set(n["capabilities"]) for n in nodes):
            raise ServiceError("unsupported_capability", "A node lacks required capabilities", 409)
        digest = content_digest(
            locked["scope_generation"], locked["key_version"], required, current
        )
        settled = None if rollback else sources.vector
        models = None if rollback else sources.models
        if automatic and digest == await self._published_digest(repo, locked):
            # Another organization's change moved a shared source revision only.
            await repo.settle(cid, locked["desired_revision"], settled, digest, models)
            return None
        rev = locked["desired_revision"] + 1
        manifest = {
            "schema_version": 1,
            "cluster_id": str(cid),
            "revision": str(rev),
            "required_capabilities": required,
            "created_at": datetime.now(UTC).isoformat(),
            "resources": current,
        }
        raw = serialize(manifest)
        sha = hashlib.sha256(raw).hexdigest()
        await repo.publish(
            cid,
            rev,
            raw,
            sha,
            sources.vector,
            sources.envelopes,
            locked["key_version"],
            actor,
            key,
            params,
        )
        changed = trigger(previous, sources.vector) if automatic else None
        await repo.settle(cid, rev, settled, digest, models, changed)
        details = {"revision": str(rev)}
        if automatic:
            details.update(actor=AUTO_PUBLISHER, trigger=changed)
        elif rollback and locked["auto_publish"]:
            await repo.pause(cid, rev)
            details["auto_publish_paused"] = True
        await repo.audit(actor, "config.released", cid, details)
        return {"revision": str(rev), "sha256": sha}

    async def release(self, token, cid, command, key):
        actor = await self.admin(token)
        key = hashlib.sha256(key.encode()).hexdigest()
        params = hashlib.sha256(serialize(command.model_dump())).hexdigest()
        async with self.tx() as repo:
            cluster = await repo.cluster(cid)
            prior = await repo.replay(cid, actor, key, params)
            if prior:
                snapshot = await repo.snapshot(cid, prior)
                return {"revision": str(prior), "sha256": snapshot["sha256"]}
            old = (
                (await repo.snapshot(cid, command.rollback_revision))["manifest"]
                if command.rollback_revision
                else None
            )
        sources = await self._read(cluster)
        if old:
            self._rollback(sources, old, command.rollback_revision)
        async with self.tx() as repo:
            locked = await repo.cluster(cid, True)
            prior = await repo.replay(cid, actor, key, params)
            if prior:
                snapshot = await repo.snapshot(cid, prior)
                return {"revision": str(prior), "sha256": snapshot["sha256"]}
            return await self._commit(
                repo,
                cluster,
                locked,
                sources,
                actor=actor,
                key=key,
                params=params,
                rollback=bool(old),
                previous=None,
            )

    async def publish_automatically(self, cluster, previous):
        """The system actor's release: the same checks as release(), never reachable by HTTP.

        Returns the new release, or None when the sources still describe the desired snapshot.
        """
        sources = await self._read(cluster)
        async with self.tx() as repo:
            return await self._commit(
                repo,
                cluster,
                await repo.cluster(cluster["id"], True),
                sources,
                actor=None,
                key=None,
                params=None,
                rollback=False,
                previous=previous,
            )

    async def revisions(self, cluster):
        """Platform-wide source revisions, read without any tenant data."""
        identity, _ = await self.identity([])
        catalog, _, _ = await self.catalog(
            [], [], cluster["public_key"], cluster["id"], cluster["key_version"]
        )
        return identity, catalog

    async def probe(self, cluster, revisions, models):
        """The source vector a release would record, without reading tenant resources."""
        billing, prices = await self.prices(models or [], cluster["deployment_ids"])
        return source_vector(cluster, *revisions, billing, prices)

    async def status(self, token, cid, rev):
        await self.admin(token)
        async with self.tx() as repo:
            cluster = await repo.cluster(cid)
            s = await repo.snapshot(cid, rev)
            job, nodes = await repo.delivery(cid, rev)
            return {
                "revision": str(rev),
                "sha256": s["sha256"],
                "desired_revision": str(cluster["desired_revision"]),
                "distributed_revision": str(cluster["distributed_revision"]),
                "distribution": job,
                "nodes": [{**n, "applied_revision": str(n["applied_revision"])} for n in nodes],
            }

    async def metrics(self):
        async with self.tx() as repo:
            return await repo.metrics()

    async def distribute(self):
        # A short retry lease survives publisher failure after the Redis write.
        async with self.tx() as repo:
            snapshot = await repo.claim_release()
            if snapshot is None:
                return 0
            cid, rev = snapshot["cluster_id"], snapshot["revision"]
            resources = snapshot["manifest"]["resources"]
            orgs = {p["organization_id"] for p in resources["quota_policies"]}
            namespaces = {org: await repo.register_limit_namespace(org) for org in sorted(orgs)}
        for org, first in namespaces.items():
            await self.cache.prepare_limits(org, first)
        await self.cache.publish_config(
            str(cid), str(rev), bytes(snapshot["payload_bytes"]), snapshot["sha256"]
        )
        async with self.tx() as repo:
            await repo.distributed(cid, rev)
        return 1
