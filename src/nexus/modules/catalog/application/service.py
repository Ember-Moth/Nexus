import json
import re
from uuid import uuid4

from nexus.shared.errors import ServiceError

from .ports import Access, Cipher, Transactions
from .schemas import DeploymentCreate, ModelCreate, UpstreamCreate, cache_dimensions


def found(value):
    if value is None:
        raise ServiceError("not_found", "Resource not found", 404)
    return value


def editable(resource, version):
    if resource.version != version:
        raise ServiceError("version_conflict", "Resource version has changed", 412)
    if resource.status == "archived":
        raise ServiceError("resource_archived", "Archived resources cannot be changed", 409)


def validated(kind, current, changes):
    values = {name: getattr(current, name) for name in kind.model_fields}
    values.update({key: value for key, value in changes.items() if key in kind.model_fields})
    try:
        return kind(**values).model_dump()
    except ValueError:
        raise ServiceError("invalid_catalog_config", "Invalid catalog configuration", 422) from None


class CatalogService:
    def __init__(self, transactions: Transactions, access: Access, cipher: Cipher):
        self._transactions = transactions
        self._access = access
        self._cipher = cipher

    async def list_models(self, token, organization_id, project_id, key_id, limit, cursor):
        scope = await self._access.visibility(token, organization_id, project_id, key_id)
        async with self._transactions() as repo:
            return await repo.list_models(
                scope.allowed_models, self._cipher.versions, limit, cursor
            )

    async def get_model(self, token, model_id, organization_id=None, project_id=None, key_id=None):
        scope = await self._access.visibility(token, organization_id, project_id, key_id)
        if scope.allowed_models is not None and model_id not in scope.allowed_models:
            raise ServiceError("not_found", "Resource not found", 404)
        async with self._transactions() as repo:
            model = found(await repo.model(model_id, self._cipher.versions))
            if scope.allowed_models is not None and (
                model.status != "active" or not model.configured
            ):
                raise ServiceError("not_found", "Resource not found", 404)
            return model

    async def create_model(self, token, body):
        actor = await self._access.administrator(token)
        async with self._transactions() as repo:
            await repo.lock()
            model = await repo.create_model(body.model_dump(), self._cipher.versions)
            await repo.audit(actor, "model.created", model.id)
            return model

    async def update_model(self, token, model_id, body, version):
        actor = await self._access.administrator(token)
        async with self._transactions() as repo:
            await repo.lock()
            current = found(await repo.model(model_id, self._cipher.versions))
            editable(current, version)
            values = validated(ModelCreate, current, body.model_dump(exclude_unset=True))
            protocols = await repo.active_protocols(model_id)
            if not protocols <= set(values["protocols"]):
                raise ServiceError(
                    "deployment_protocol_conflict",
                    "Disable incompatible deployments before changing protocols",
                    409,
                )
            if {"token_dimensions", "tool_dimensions"} & body.model_fields_set:
                for profile in await repo.active_relay_profiles(model_id):
                    if profile.get("bedrock_guardrail_unit_ceiling") and not any(
                        k.startswith("guardrail_") for k in values["tool_dimensions"]
                    ):
                        raise ServiceError(
                            "price_dimensions_mismatch",
                            "Active Guardrail pricing requires policy units",
                            422,
                        )
                    if profile.get("container_reserve_minutes") and not any(
                        k.startswith("container_") for k in values["tool_dimensions"]
                    ):
                        raise ServiceError(
                            "price_dimensions_mismatch",
                            "Active containers require a priced memory tier",
                            422,
                        )
                    if (
                        profile.get("execution_reserve_seconds")
                        and "code_execution_seconds" not in values["tool_dimensions"]
                    ):
                        raise ServiceError(
                            "price_dimensions_mismatch",
                            "Standalone code execution needs time pricing",
                            422,
                        )
                    if profile.get("file_search_ceiling") and not {
                        "file_search_calls",
                        "vector_storage_byte_seconds",
                    } <= set(values["tool_dimensions"]):
                        raise ServiceError(
                            "price_dimensions_mismatch",
                            "Active File Search requires search and storage pricing",
                            422,
                        )
                    if (
                        profile.get("web_search_ceiling")
                        and "web_search_requests" not in values["tool_dimensions"]
                    ):
                        raise ServiceError(
                            "price_dimensions_mismatch",
                            "Active Web Search deployments require search pricing",
                            422,
                        )
                    if profile.get("auto_prompt_cache") and not cache_dimensions(
                        profile["endpoint"]
                    ) <= set(values["token_dimensions"]):
                        raise ServiceError(
                            "price_dimensions_mismatch",
                            "Active caching deployments require cache pricing dimensions",
                            422,
                        )
            if body.status:
                values["status"] = body.status
            result = await repo.update_model(model_id, values, self._cipher.versions)
            await repo.audit(
                actor,
                "model.updated",
                model_id,
                {
                    "before": current.model_dump(mode="json"),
                    "after": result.model_dump(mode="json"),
                },
            )
            return result

    async def list_upstreams(self, token, limit, cursor):
        await self._access.administrator(token)
        async with self._transactions() as repo:
            return await repo.upstreams(limit, cursor)

    async def get_upstream(self, token, upstream_id):
        await self._access.administrator(token)
        async with self._transactions() as repo:
            return found(await repo.upstream(upstream_id))

    async def create_upstream(self, token, body):
        actor = await self._access.administrator(token)
        async with self._transactions() as repo:
            await repo.lock()
            result = await repo.create_upstream(body.model_dump())
            await repo.audit(actor, "upstream.created", result.id)
            return result

    async def update_upstream(self, token, upstream_id, body, version):
        actor = await self._access.administrator(token)
        async with self._transactions() as repo:
            await repo.lock()
            current = found(await repo.upstream(upstream_id))
            editable(current, version)
            values = validated(UpstreamCreate, current, body.model_dump(exclude_unset=True))
            if values["base_url"] != current.base_url and current.status != "disabled":
                raise ServiceError(
                    "upstream_must_be_disabled",
                    "Disable the upstream before changing its endpoint",
                    409,
                )
            if body.status:
                values["status"] = body.status
            result = await repo.update_upstream(upstream_id, values)
            await repo.audit(
                actor,
                "upstream.updated",
                upstream_id,
                {
                    "before": current.model_dump(mode="json"),
                    "after": result.model_dump(mode="json"),
                },
            )
            return result

    async def list_credentials(self, token, upstream_id, limit, cursor):
        await self._access.administrator(token)
        async with self._transactions() as repo:
            found(await repo.upstream(upstream_id))
            return await repo.credentials(upstream_id, limit, cursor)

    async def get_credential(self, token, upstream_id, credential_id):
        await self._access.administrator(token)
        async with self._transactions() as repo:
            return found(await repo.credential(upstream_id, credential_id))

    async def create_credential(self, token, upstream_id, body):
        actor = await self._access.administrator(token)
        credential_id = uuid4()
        key_version, ciphertext = self._cipher.encrypt(
            body.secret.get_secret_value(), upstream_id, credential_id
        )
        async with self._transactions() as repo:
            await repo.lock()
            upstream = found(await repo.upstream(upstream_id))
            if upstream.status == "archived":
                raise ServiceError(
                    "resource_archived", "Archived upstreams cannot receive credentials", 409
                )
            if (
                upstream.protocol_family == "aws_bedrock"
                and body.secret.get_secret_value().startswith("{")
            ):
                try:
                    material = json.loads(body.secret.get_secret_value())
                    valid = (
                        isinstance(material, dict)
                        and {"access_key_id", "secret_access_key"} <= material.keys()
                        and material.keys()
                        <= {"access_key_id", "secret_access_key", "session_token"}
                        and all(
                            isinstance(v, str)
                            and v
                            and v.isascii()
                            and all(33 <= ord(c) <= 126 for c in v)
                            for v in material.values()
                        )
                    )
                except ValueError:
                    valid = False
                if not valid:
                    raise ServiceError(
                        "invalid_credential", "Invalid Bedrock signing credential", 422
                    )
            result = await repo.create_credential(
                credential_id, upstream_id, body.name, key_version, ciphertext
            )
            await repo.audit(
                actor,
                "upstream_credential.created",
                credential_id,
                {"upstream_id": str(upstream_id), "key_version": key_version},
            )
            return result

    async def revoke_credential(self, token, upstream_id, credential_id, version):
        actor = await self._access.administrator(token)
        async with self._transactions() as repo:
            await repo.lock()
            credential = found(await repo.credential(upstream_id, credential_id))
            editable(credential, version)
            if credential.status == "revoked":
                return credential
            result = await repo.revoke_credential(upstream_id, credential_id)
            await repo.audit(
                actor,
                "upstream_credential.revoked",
                credential_id,
                {"upstream_id": str(upstream_id)},
            )
            return result

    async def list_deployments(self, token, model_id, upstream_id, limit, cursor):
        await self._access.administrator(token)
        async with self._transactions() as repo:
            return await repo.deployments(
                self._cipher.versions, model_id, upstream_id, limit, cursor
            )

    async def get_deployment(self, token, deployment_id):
        await self._access.administrator(token)
        async with self._transactions() as repo:
            return found(await repo.deployment(deployment_id, self._cipher.versions))

    async def _validate_deployment(self, repo, values):
        model = found(await repo.model(values["model_id"], self._cipher.versions))
        upstream = found(await repo.upstream(values["upstream_id"]))
        credential = found(await repo.credential(upstream.id, values["credential_id"]))
        if upstream.protocol_family not in model.protocols:
            raise ServiceError(
                "protocol_mismatch", "Upstream protocol is not supported by the public model", 409
            )
        if profile := values.get("relay_profile"):
            if profile.get("bedrock_guardrail_unit_ceiling") and not any(
                k.startswith("guardrail_") for k in model.tool_dimensions
            ):
                raise ServiceError(
                    "price_dimensions_mismatch",
                    "Configure Guardrail policy dimensions on the model",
                    422,
                )
            if profile.get("container_reserve_minutes") and not any(
                k.startswith("container_") for k in model.tool_dimensions
            ):
                raise ServiceError(
                    "price_dimensions_mismatch",
                    "Configure container memory-tier pricing before enabling the deployment",
                    422,
                )
            if (
                profile.get("execution_reserve_seconds")
                and "code_execution_seconds" not in model.tool_dimensions
            ):
                raise ServiceError(
                    "price_dimensions_mismatch",
                    "Configure code_execution_seconds for standalone execution",
                    422,
                )
            if profile.get("file_search_ceiling") and not {
                "file_search_calls",
                "vector_storage_byte_seconds",
            } <= set(model.tool_dimensions):
                raise ServiceError(
                    "price_dimensions_mismatch",
                    "Configure File Search call and storage pricing before enabling the deployment",
                    422,
                )
            if (
                profile.get("web_search_ceiling")
                and "web_search_requests" not in model.tool_dimensions
            ):
                raise ServiceError(
                    "price_dimensions_mismatch",
                    "Configure web_search_requests before enabling Web Search",
                    422,
                )
            if profile.get("auto_prompt_cache") and not cache_dimensions(
                profile["endpoint"]
            ) <= set(model.token_dimensions):
                raise ServiceError(
                    "price_dimensions_mismatch",
                    "Configure cache read and write dimensions before enabling automatic caching",
                    422,
                )
            family = {
                "openai_responses": "openai_compatible",
                "openai_chat_completions": "openai_compatible",
                "anthropic_messages": "anthropic",
                "bedrock_converse": "aws_bedrock",
            }
            if profile["endpoint"] == "bedrock_converse" and not re.fullmatch(
                r"[a-z]{2}(?:-[a-z0-9]+)+-[0-9]+", values["region"]
            ):
                raise ServiceError(
                    "invalid_region", "Bedrock deployments require an AWS region", 422
                )
            if family[profile["endpoint"]] != upstream.protocol_family:
                raise ServiceError(
                    "protocol_mismatch", "Relay endpoint conflicts with upstream", 409
                )
            if (
                profile["input_ceiling"] > model.max_input_tokens
                or profile["output_ceiling"] > model.max_output_tokens
            ):
                raise ServiceError("invalid_bounds", "Deployment exceeds public model limits", 422)
        if credential.status != "active" or not self._cipher.supports(credential.key_version):
            raise ServiceError(
                "credential_not_ready",
                "An active credential with an available encryption key is required",
                409,
            )
        if model.status == "archived" or upstream.status == "archived":
            raise ServiceError(
                "resource_archived", "Archived parents cannot receive deployments", 409
            )
        if values["status"] == "active" and (
            model.status != "active" or upstream.status != "active"
        ):
            raise ServiceError("parent_inactive", "Active model and upstream are required", 409)

    async def create_deployment(self, token, body):
        actor = await self._access.administrator(token)
        async with self._transactions() as repo:
            await repo.lock()
            values = body.model_dump()
            await self._validate_deployment(repo, values)
            result = await repo.create_deployment(values, self._cipher.versions)
            await repo.audit(actor, "deployment.created", result.id)
            return result

    async def update_deployment(self, token, deployment_id, body, version):
        actor = await self._access.administrator(token)
        async with self._transactions() as repo:
            await repo.lock()
            current = found(await repo.deployment(deployment_id, self._cipher.versions))
            editable(current, version)
            changes = body.model_dump(exclude_unset=True)
            # Archive keeps its historical binding, even when its credential is already revoked.
            candidate = current.model_dump(include=set(DeploymentCreate.model_fields))
            if (
                current.relay_profile
                and changes.get("upstream_model", current.upstream_model) != current.upstream_model
                and "relay_profile" not in changes
            ):
                raise ServiceError("relay_profile_required", "Revalidate the new model bounds", 422)
            candidate.update(changes)
            if candidate["status"] == "archived":
                if set(changes) != {"status"}:
                    raise ServiceError(
                        "invalid_archive", "Archive must be a standalone status change", 422
                    )
            elif changes == {"status": "disabled"}:
                # Invalid credentials must not prevent an operator from disabling a mapping.
                pass
            else:
                try:
                    candidate = DeploymentCreate(**candidate).model_dump()
                except ValueError:
                    raise ServiceError(
                        "invalid_deployment", "Invalid deployment configuration", 422
                    ) from None
                await self._validate_deployment(repo, candidate)
            result = await repo.update_deployment(deployment_id, candidate, self._cipher.versions)
            await repo.audit(
                actor,
                "deployment.updated",
                deployment_id,
                {
                    "before": current.model_dump(mode="json"),
                    "after": result.model_dump(mode="json"),
                },
            )
            return result
