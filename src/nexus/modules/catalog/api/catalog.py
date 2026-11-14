from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response

from nexus.http import credentials, cursor_value, error_responses, expected_version

from ..application.schemas import (
    CredentialCreate,
    CredentialView,
    DeploymentCreate,
    DeploymentPatch,
    DeploymentView,
    ModelCreate,
    ModelPatch,
    ModelView,
    Page,
    UpstreamCreate,
    UpstreamPatch,
    UpstreamView,
)
from ..application.service import CatalogService

CONFLICT = error_responses(409)


def create_router(get_service):
    router = APIRouter(prefix="/api/v1", tags=["catalog"])
    Service = Annotated[CatalogService, Depends(get_service)]
    Token = Annotated[str, Depends(credentials)]
    Version = Annotated[int, Depends(expected_version)]
    Limit = Annotated[int, Query(ge=1, le=100)]
    Cursor = Annotated[str | None, Query(max_length=512)]

    def tagged(response, result):
        response.headers["ETag"] = f'"{result.version}"'
        return result

    @router.get("/models", response_model=Page[ModelView])
    async def models(
        token: Token,
        service: Service,
        organization_id: UUID | None = None,
        project_id: UUID | None = None,
        key_id: UUID | None = None,
        limit: Limit = 50,
        cursor: Cursor = None,
    ):
        return await service.list_models(
            token, organization_id, project_id, key_id, limit, cursor_value(cursor)
        )

    @router.get("/models/{model_id}", response_model=ModelView)
    async def model(
        model_id: UUID,
        response: Response,
        token: Token,
        service: Service,
        organization_id: UUID | None = None,
        project_id: UUID | None = None,
        key_id: UUID | None = None,
    ):
        return tagged(
            response, await service.get_model(token, model_id, organization_id, project_id, key_id)
        )

    @router.post("/models", response_model=ModelView, status_code=201, responses=CONFLICT)
    async def create_model(body: ModelCreate, response: Response, token: Token, service: Service):
        return tagged(response, await service.create_model(token, body))

    @router.patch("/models/{model_id}", response_model=ModelView, responses=CONFLICT)
    async def update_model(
        model_id: UUID,
        body: ModelPatch,
        response: Response,
        version: Version,
        token: Token,
        service: Service,
    ):
        return tagged(response, await service.update_model(token, model_id, body, version))

    @router.get("/upstreams", response_model=Page[UpstreamView])
    async def upstreams(token: Token, service: Service, limit: Limit = 50, cursor: Cursor = None):
        return await service.list_upstreams(token, limit, cursor_value(cursor))

    @router.get("/upstreams/{upstream_id}", response_model=UpstreamView)
    async def upstream(upstream_id: UUID, response: Response, token: Token, service: Service):
        return tagged(response, await service.get_upstream(token, upstream_id))

    @router.post("/upstreams", response_model=UpstreamView, status_code=201, responses=CONFLICT)
    async def create_upstream(
        body: UpstreamCreate, response: Response, token: Token, service: Service
    ):
        return tagged(response, await service.create_upstream(token, body))

    @router.patch("/upstreams/{upstream_id}", response_model=UpstreamView, responses=CONFLICT)
    async def update_upstream(
        upstream_id: UUID,
        body: UpstreamPatch,
        response: Response,
        version: Version,
        token: Token,
        service: Service,
    ):
        return tagged(response, await service.update_upstream(token, upstream_id, body, version))

    @router.get("/upstreams/{upstream_id}/credentials", response_model=Page[CredentialView])
    async def credentials_list(
        upstream_id: UUID, token: Token, service: Service, limit: Limit = 50, cursor: Cursor = None
    ):
        return await service.list_credentials(token, upstream_id, limit, cursor_value(cursor))

    @router.get(
        "/upstreams/{upstream_id}/credentials/{credential_id}", response_model=CredentialView
    )
    async def credential(
        upstream_id: UUID, credential_id: UUID, response: Response, token: Token, service: Service
    ):
        return tagged(response, await service.get_credential(token, upstream_id, credential_id))

    @router.post(
        "/upstreams/{upstream_id}/credentials",
        response_model=CredentialView,
        status_code=201,
        responses=CONFLICT,
    )
    async def create_credential(
        upstream_id: UUID,
        body: CredentialCreate,
        response: Response,
        token: Token,
        service: Service,
    ):
        return tagged(response, await service.create_credential(token, upstream_id, body))

    @router.post(
        "/upstreams/{upstream_id}/credentials/{credential_id}/revoke",
        response_model=CredentialView,
        responses=CONFLICT,
    )
    async def revoke_credential(
        upstream_id: UUID,
        credential_id: UUID,
        response: Response,
        version: Version,
        token: Token,
        service: Service,
    ):
        return tagged(
            response, await service.revoke_credential(token, upstream_id, credential_id, version)
        )

    @router.get("/deployments", response_model=Page[DeploymentView])
    async def deployments(
        token: Token,
        service: Service,
        model_id: UUID | None = None,
        upstream_id: UUID | None = None,
        limit: Limit = 50,
        cursor: Cursor = None,
    ):
        return await service.list_deployments(
            token, model_id, upstream_id, limit, cursor_value(cursor)
        )

    @router.get("/deployments/{deployment_id}", response_model=DeploymentView)
    async def deployment(deployment_id: UUID, response: Response, token: Token, service: Service):
        return tagged(response, await service.get_deployment(token, deployment_id))

    @router.post("/deployments", response_model=DeploymentView, status_code=201, responses=CONFLICT)
    async def create_deployment(
        body: DeploymentCreate, response: Response, token: Token, service: Service
    ):
        return tagged(response, await service.create_deployment(token, body))

    @router.patch("/deployments/{deployment_id}", response_model=DeploymentView, responses=CONFLICT)
    async def update_deployment(
        deployment_id: UUID,
        body: DeploymentPatch,
        response: Response,
        version: Version,
        token: Token,
        service: Service,
    ):
        return tagged(
            response, await service.update_deployment(token, deployment_id, body, version)
        )

    return router
