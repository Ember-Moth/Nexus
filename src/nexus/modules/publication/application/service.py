import base64
import hashlib
import json
from datetime import UTC, datetime
from uuid import uuid4

from nexus.shared.errors import ServiceError

from .schemas import CAPABILITIES


def serialize(value):
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str
    ).encode()


class PublicationService:
    def __init__(self, transactions, administrator, identity, catalog, prices, cache):
        self.tx = transactions
        self.admin = administrator
        self.identity = identity
        self.catalog = catalog
        self.prices = prices
        self.cache = cache

    @staticmethod
    def public(cluster):
        return {
            **cluster,
            "public_key": base64.b64encode(cluster["public_key"]).decode(),
            "desired_revision": str(cluster["desired_revision"]),
            "distributed_revision": str(cluster["distributed_revision"]),
        }

    async def create(self, token, body):
        actor = await self.admin(token)
        cid = uuid4()
        async with self.tx() as repo:
            await repo.create_cluster(cid, body, base64.b64decode(body.public_key))
            await repo.audit(actor, "cluster.created", cid)
            return self.public(await repo.cluster(cid))

    async def clusters(self, token):
        await self.admin(token)
        async with self.tx() as repo:
            return [self.public(row) for row in await repo.list_clusters()]

    async def update(self, token, cid, body, version):
        actor = await self.admin(token)
        async with self.tx() as repo:
            c = await repo.cluster(cid, True)
            if c["version"] != version:
                raise ServiceError("version_conflict", "Cluster version changed", 412)
            scope_changed = any(
                getattr(body, k) is not None and not set(c[k]) <= set(getattr(body, k))
                for k in ("organization_ids", "deployment_ids")
            )
            new_key = base64.b64decode(body.public_key) if body.public_key else None
            await repo.update_cluster(c, body, new_key, scope_changed)
            await repo.audit(actor, "cluster.updated", cid, {"previous_version": version})
            return self.public(await repo.cluster(cid))

    async def principal(self, token, cid, body):
        actor = await self.admin(token)
        async with self.tx() as repo:
            await repo.cluster(cid, True)
            await repo.register(cid, body)
            await repo.audit(actor, "principal.configured", body.node_id, {"cluster_id": str(cid)})
            return body

    async def _sources(self, c):
        orgs = c["organization_ids"]
        deps = c["deployment_ids"]
        identity_rev, identity = await self.identity(orgs)
        catalog_rev, catalog, envelopes = await self.catalog(
            orgs, deps, c["public_key"], c["id"], c["key_version"]
        )
        models = [m["id"] for m in catalog["models"]]
        pricing_rev, prices = await self.prices(models, deps)
        for model in catalog["models"]:
            price = prices.get(("sale", model["id"]))
            if not price or price[1] != set(model["token_dimensions"]):
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
        vector = {
            "scope_generation": c["scope_generation"],
            "key_version": c["key_version"],
            "identity": identity_rev,
            "catalog": catalog_rev,
            "billing": pricing_rev,
            "effective_prices": sorted(v[0] for v in prices.values()),
        }
        return vector, json.loads(serialize({**identity, **catalog})), envelopes

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
        for _ in range(3):
            try:
                sources, current, envelopes = await self._sources(cluster)
                check, again, _ = await self._sources(cluster)
            except ValueError as error:
                raise ServiceError("invalid_snapshot", str(error), 422) from None
            if sources == check and current == again:
                break
        else:
            raise ServiceError("source_changed", "Configuration changed during publication", 409)
        if old:
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
            if any(
                r["organization_id"] not in set(map(str, cluster["organization_ids"]))
                for r in old_resources["routes"] + old_resources["quota_policies"]
            ):
                raise ServiceError(
                    "rollback_scope_changed", "Historical tenant scope has changed", 409
                )
            current = old_resources
            selected = {r["credential_id"] for r in current["credential_refs"]}
            envelopes = {k: v for k, v in envelopes.items() if str(k) in selected}
            sources["rollback_revision"] = str(command.rollback_revision)
        async with self.tx() as repo:
            locked = await repo.cluster(cid, True)
            prior = await repo.replay(cid, actor, key, params)
            if prior:
                snapshot = await repo.snapshot(cid, prior)
                return {"revision": str(prior), "sha256": snapshot["sha256"]}
            if (
                locked["version"] != cluster["version"]
                or locked["desired_revision"] != cluster["desired_revision"]
                or not locked["active"]
            ):
                raise ServiceError("cluster_changed", "Cluster changed during publication", 409)
            nodes = await repo.capabilities(cid)
            required = list(CAPABILITIES)
            profiles = [d["relay_profile"] for d in current["deployments"] if "relay_profile" in d]
            if any(p["endpoint"] == "bedrock_converse" for p in profiles):
                required.append("relay.bedrock-converse.v1")
            if profiles:
                required.append("relay.native.v1")
            if any(p.get("streaming") and "http" in p["transports"] for p in profiles):
                required.append("relay.sse.v1")
            if any("websocket" in p["transports"] for p in profiles):
                required.append("relay.responses-ws.v1")
            for source, capability in [
                ("anthropic_messages", "relay.messages-to-responses.v1"),
                ("openai_responses", "relay.responses-to-messages.v1"),
            ]:
                if any(p.get("convert_from") == source for p in profiles):
                    required.append(capability)
            if any(not set(required) <= set(n["capabilities"]) for n in nodes):
                raise ServiceError(
                    "unsupported_capability", "A node lacks required capabilities", 409
                )
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
                sources,
                envelopes,
                locked["key_version"],
                actor,
                key,
                params,
            )
            await repo.audit(actor, "config.released", cid, {"revision": str(rev)})
            return {"revision": str(rev), "sha256": sha}

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
