"""Upstream copies of tenant content held for API keys, listed and purged on request.

Identity decides which keys a caller may see or purge (ResourceAccess); publication owns the
copies and the purge queue the Relay nodes work through.
"""

import hashlib
from uuid import uuid4

from nexus.shared.errors import ServiceError

from .ports import ResourceAccess
from .schemas import HANDLE_PREFIXES
from .service import serialize

PURGE_STATES = {"pending": "pending", "done": "purged", "abandoned": "failed"}


def purge_view(row):
    if row["purge_id"] is None:
        return None
    pending = row["purge_state"] == "pending"
    return {
        "id": row["purge_id"],
        "state": PURGE_STATES[row["purge_state"]],
        "reason": row["reason"],
        "attempts": row["attempts"],
        "last_error": row["last_error"],
        "created_at": row["purge_created_at"],
        "next_attempt_at": row["next_attempt"] if pending else None,
        "finished_at": row["finished_at"],
    }


def resource_view(row, owners):
    owner = owners.get(row["api_key_id"])
    return {
        "id": row["id"],
        "handle": HANDLE_PREFIXES[row["kind"]] + str(row["id"]) if row["bound"] else None,
        "kind": row["kind"],
        "state": row["state"],
        "organization_id": owner and owner.organization_id,
        "project_id": owner and owner.project_id,
        "api_key_id": row["api_key_id"],
        "cluster_id": row["cluster_id"],
        "deployment_id": row["deployment_id"],
        "created_at": row["created_at"],
        "purge": purge_view(row),
    }


def request_view(row):
    state = "pending" if row["pending"] else "failed" if row["failed"] else "purged"
    return {
        "id": row["id"],
        "organization_id": row["organization_id"],
        "api_key_id": row["api_key_id"],
        "kind": row["kind"],
        "resource_id": row["resource_id"],
        "requested_by": row["actor_id"],
        "created_at": row["created_at"],
        "state": state,
        "total": row["total"],
        "pending": row["pending"],
        "purged": row["purged"],
        "failed": row["failed"],
    }


class UpstreamResourceService:
    def __init__(self, transactions, access: ResourceAccess):
        self.tx = transactions
        self.access = access

    async def list(
        self,
        token,
        organization_id,
        *,
        project_id=None,
        api_key_id=None,
        kind=None,
        state=None,
        limit=50,
        cursor=None,
    ):
        """Copies held for the organization's keys, revoked ones included, newest first.

        Without an organization only a platform administrator may list, across every key.
        """
        if project_id and not organization_id:
            raise ServiceError("organization_required", "organization_id is required", 422)
        scope = await self.access.scope(
            token, organization_id, project_id=project_id, api_key_id=api_key_id
        )
        keys = list(scope.keys) if scope.keys is not None else [api_key_id] if api_key_id else None
        if keys == []:
            return {"items": [], "next_cursor": None}
        async with self.tx() as repo:
            rows, next_cursor = await repo.resources(keys, kind, state, limit, cursor)
        owners = (
            scope.keys
            if scope.keys is not None
            else await self.access.owners({row["api_key_id"] for row in rows})
        )
        return {"items": [resource_view(row, owners) for row in rows], "next_cursor": next_cursor}

    async def purge(self, token, organization_id, command, idempotency_key):
        """Queue deletion of the key's copies, or one of them; returns the request to poll.

        Replaying the Idempotency-Key returns the original request. A copy already queued by
        the grace-period schedule or an earlier request keeps its purge; one that gave up is
        retried.
        """
        scope = await self.access.scope(
            token, organization_id, api_key_id=command.api_key_id, manage=True
        )
        key = hashlib.sha256(idempotency_key.encode()).hexdigest()
        parameters = hashlib.sha256(serialize(command.model_dump(mode="json"))).hexdigest()
        async with self.tx() as repo:
            row = await repo.request_purge(
                uuid4(), organization_id, scope.actor_id, command, key, parameters
            )
        return request_view(row)

    async def purge_request(self, token, organization_id, request_id):
        scope = await self.access.scope(token, organization_id)
        async with self.tx() as repo:
            row = await repo.request(request_id, organization_id)
        if row["api_key_id"] not in scope.keys:
            raise ServiceError("not_found", "Resource not found", 404)
        return request_view(row)
