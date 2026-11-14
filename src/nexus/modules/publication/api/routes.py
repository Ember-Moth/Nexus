from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Response

from nexus.http import credentials, error_responses, expected_version, idempotency_key
from nexus.shared.errors import ServiceError

from ..application.schemas import (
    Cluster,
    ClusterUpdate,
    ClusterView,
    Principal,
    Release,
    ReleaseAccepted,
    ReleaseStatus,
)
from ..application.service import PublicationService

CONFLICT = error_responses(409)


def create_router(get_service):
    router = APIRouter(prefix="/api/v1/gateway-clusters", tags=["gateway publication"])
    Token = Annotated[str, Depends(credentials)]
    Service = Annotated[PublicationService, Depends(get_service)]

    @router.get("")
    async def clusters(token: Token, service: Service):
        return {"items": await service.clusters(token)}

    @router.post("", status_code=201, response_model=ClusterView, responses=CONFLICT)
    async def cluster(body: Cluster, token: Token, service: Service, response: Response):
        result = await service.create(token, body)
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
