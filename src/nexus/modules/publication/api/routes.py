import re
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response

from nexus.http import (
    credentials,
    error_responses,
    expected_version,
    idempotency_key,
    key_cursor_value,
)
from nexus.shared.errors import ServiceError

from ..application.schemas import (
    Cluster,
    ClusterUpdate,
    ClusterView,
    Page,
    Principal,
    PrincipalView,
    Release,
    ReleaseAccepted,
    ReleaseRecord,
    ReleaseStatus,
)
from ..application.service import PublicationService

CONFLICT = error_responses(409)


def revision_key(value: str) -> int:
    """A cursor position in release history: a positive bigint revision as a decimal string."""
    if not re.fullmatch(r"[1-9][0-9]{0,18}", value) or int(value) >= 2**63:
        raise ValueError
    return int(value)


def create_router(get_service):
    router = APIRouter(prefix="/api/v1/gateway-clusters", tags=["gateway publication"])
    Token = Annotated[str, Depends(credentials)]
    Service = Annotated[PublicationService, Depends(get_service)]
    Limit = Annotated[int, Query(ge=1, le=100)]
    Cursor = Annotated[str | None, Query(max_length=512)]

    @router.get("", response_model=Page[ClusterView])
    async def clusters(
        token: Token,
        service: Service,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        cursor: Annotated[str | None, Query(max_length=512)] = None,
    ):
        """Gateway clusters ordered by id."""
        return await service.clusters(token, limit, key_cursor_value(cursor, 1))

    @router.post("", status_code=201, response_model=ClusterView, responses=CONFLICT)
    async def cluster(body: Cluster, token: Token, service: Service, response: Response):
        result = await service.create(token, body)
        response.headers["ETag"] = f'"{result["version"]}"'
        return result

    @router.get("/{cluster_id}", response_model=ClusterView)
    async def read_cluster(cluster_id: UUID, response: Response, token: Token, service: Service):
        """One gateway cluster; its ETag is the If-Match for the next PATCH."""
        result = await service.cluster(token, cluster_id)
        response.headers["ETag"] = f'"{result["version"]}"'
        return result

    @router.patch("/{cluster_id}", response_model=ClusterView, responses=CONFLICT)
    async def update(
        cluster_id: UUID,
        body: ClusterUpdate,
        response: Response,
        token: Token,
        version: Annotated[int, Depends(expected_version)],
        service: Service,
    ):
        result = await service.update(token, cluster_id, body, version)
        response.headers["ETag"] = f'"{result["version"]}"'
        return result

    @router.put("/{cluster_id}/principals/{node_id}", response_model=Principal, responses=CONFLICT)
    async def principal(
        cluster_id: UUID, node_id: UUID, body: Principal, token: Token, service: Service
    ):
        if node_id != body.node_id:
            raise ServiceError("node_mismatch", "Node identifiers differ", 422)
        return await service.principal(token, cluster_id, body)

    @router.get("/{cluster_id}/principals", response_model=Page[PrincipalView])
    async def principals(
        cluster_id: UUID, token: Token, service: Service, limit: Limit = 50, cursor: Cursor = None
    ):
        """Registered gateway nodes of the cluster ordered by node id."""
        return await service.principals(token, cluster_id, limit, key_cursor_value(cursor, 1))

    @router.get("/{cluster_id}/releases", response_model=Page[ReleaseRecord])
    async def releases(
        cluster_id: UUID, token: Token, service: Service, limit: Limit = 50, cursor: Cursor = None
    ):
        """Configuration releases of the cluster, newest revision first."""
        return await service.releases(
            token, cluster_id, limit, key_cursor_value(cursor, 1, revision_key)
        )

    @router.post("/{cluster_id}/releases", status_code=202, response_model=ReleaseAccepted)
    async def release(
        cluster_id: UUID,
        body: Release,
        token: Token,
        key: Annotated[str, Depends(idempotency_key)],
        service: Service,
    ):
        return await service.release(token, cluster_id, body, key)

    @router.get("/{cluster_id}/releases/{revision}", response_model=ReleaseStatus)
    async def status(cluster_id: UUID, revision: int, token: Token, service: Service):
        return await service.status(token, cluster_id, revision)

    return router
