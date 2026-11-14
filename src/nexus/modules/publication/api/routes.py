from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Response

from nexus.http import credentials, expected_version, idempotency_key
from nexus.shared.errors import ServiceError

from ..application.schemas import Cluster, ClusterUpdate, Principal, Release


def create_router(get_service):
    router = APIRouter(prefix="/api/v1/gateway-clusters", tags=["gateway publication"])
    Token = Annotated[str, Depends(credentials)]
    Service = Annotated[object, Depends(get_service)]

    @router.get("")
    async def clusters(token: Token, service: Service):
        return {"items": await service.clusters(token)}

    @router.post("", status_code=201)
    async def cluster(body: Cluster, token: Token, service: Service, response: Response):
        result = await service.create(token, body)
        response.headers["ETag"] = f'"{result["version"]}"'
        return result

    @router.patch("/{cid}")
    async def update(
        cid: UUID,
        body: ClusterUpdate,
        response: Response,
        token: Token,
        version: Annotated[int, Depends(expected_version)],
        service: Service,
    ):
        result = await service.update(token, cid, body, version)
        response.headers["ETag"] = f'"{result["version"]}"'
        return result

    @router.put("/{cid}/principals/{node_id}")
    async def principal(cid: UUID, node_id: UUID, body: Principal, token: Token, service: Service):
        if node_id != body.node_id:
            raise ServiceError("node_mismatch", "Node identifiers differ", 422)
        return await service.principal(token, cid, body)

    @router.post("/{cid}/releases", status_code=202)
    async def release(
        cid: UUID,
        body: Release,
        token: Token,
        key: Annotated[str, Depends(idempotency_key)],
        service: Service,
    ):
        return await service.release(token, cid, body, key)

    @router.get("/{cid}/releases/{revision}")
    async def status(cid: UUID, revision: int, token: Token, service: Service):
        return await service.status(token, cid, revision)

    return router
