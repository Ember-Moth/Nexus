//! Authenticated discovery and provider token counts; neither is a generation.
use super::{Node, error};
use crate::{
    control::{
        limits::{Admission, LimitError},
        snapshot::Index,
    },
    protocol::{
        Endpoint,
        request::{Generate, Operation, Route},
        tokens::ReservationBounds,
    },
    request::{Failure, lifecycle::credential},
    upstream::read_body,
};
use axum::{
    Extension, Json,
    extract::State,
    response::{IntoResponse, Response},
};
use http::{HeaderMap, StatusCode};
use serde_json::{Value, json};
use std::sync::Arc;
use uuid::Uuid;

pub(super) async fn models(
    State(node): State<Arc<Node>>,
    Extension(id): Extension<Uuid>,
    headers: HeaderMap,
) -> Response {
    let ep = Endpoint::OpenaiResponses;
    let result = async {
        let secret = Route::Models
            .key(&headers)
            .map_err(|_| Failure(StatusCode::UNAUTHORIZED, "invalid_api_key"))?;
        let index = node.current().ok_or_else(Failure::unavailable)?;
        let _permit = node
            .capacity
            .try_acquire()
            .map_err(|_| Failure::unavailable())?;
        let key = index.authenticate(secret)?;
        Ok::<_, Failure>(
            node.db
                .preflight(key.id, index.revision, None, None)
                .await?,
        )
    }
    .await;
    match result {
        Ok(v) => Json(v).into_response(),
        Err(e) => error(id, ep, e),
    }
}

pub(super) async fn count(
    node: &Node,
    index: &Index,
    secret: &str,
    id: Uuid,
    req: Generate,
) -> Result<Response, Failure> {
    let s = index.select(secret, &req)?;
    node.db
        .preflight(
            s.key.id,
            index.revision,
            Some(s.model),
            Some(s.deployment.id),
        )
        .await?;
    let material = credential(node, index, s.deployment).await?;
    let send = async {
        let bedrock = s.deployment.is_bedrock();
        let upstream = if bedrock {
            crate::bedrock::transport::count(
                &node.upstream.client,
                s.deployment,
                &material,
                &s.body,
            )
            .await
        } else {
            node.upstream
                .open(
                    crate::upstream::Upstream::endpoint(
                        &s.deployment.base_url,
                        Endpoint::AnthropicMessages,
                        Operation::CountTokens,
                    ),
                    Endpoint::AnthropicMessages,
                    &material,
                    &s.body,
                    false,
                    &req.anthropic,
                )
                .await
        }
        .map_err(|_| Failure::upstream())?;
        let status = upstream.status();
        let bytes = read_body(upstream, node.upstream.response_limit())
            .await
            .map_err(|_| Failure::upstream())?;
        let data: Value = serde_json::from_slice(&bytes).map_err(|_| Failure::upstream())?;
        if status.is_client_error() || status.is_server_error() {
            if bedrock {
                return Ok((
                    status,
                    Json(crate::bedrock::transport::error_body(status, &data)),
                )
                    .into_response());
            }
            return Ok((status, Json(data)).into_response());
        }
        let tokens = data[if bedrock {
            "inputTokens"
        } else {
            "input_tokens"
        }]
        .as_u64()
        .ok_or_else(Failure::upstream)?;
        if !status.is_success() {
            return Err(Failure::upstream());
        }
        Ok(Json(json!({"input_tokens":tokens})).into_response())
    };
    with_quota(node, index, s.key, s.model, id, send).await
}

pub(super) async fn with_quota<T>(
    node: &Node,
    index: &Index,
    key: &crate::control::snapshot::Key,
    model: Uuid,
    id: Uuid,
    send: impl std::future::Future<Output = Result<T, Failure>>,
) -> Result<T, Failure> {
    let mut lease = node
        .limits
        .acquire(Admission {
            key,
            request: id,
            node: index.node,
            model,
            revision: index.revision,
            bounds: ReservationBounds {
                guardrail_units: 0,
                searches: 0,
                file_searches: 0,
                interpreter: None,
                execution_mode: crate::protocol::execution::ExecutionMode::None,
                execution_seconds: 0,
                inputs: 0,
                outputs: 0,
            },
            quotas: &index.quotas,
            ttl_ms: node.lease_ms,
        })
        .await
        .map_err(|e| match e {
            LimitError::Limited => Failure(StatusCode::TOO_MANY_REQUESTS, "rate_limited"),
            _ => Failure::unavailable(),
        })?;
    tokio::pin!(send);
    let result = loop {
        tokio::select! {
            r=&mut send=>break r,
            _=tokio::time::sleep_until(lease.renewal_at())=>{
                if node.limits.renew(&mut lease).await.is_err(){break Err(Failure::unavailable());}
            }
        }
    };
    node.limits
        .finish(&lease, Some(0))
        .await
        .map_err(|_| Failure::unavailable())?;
    result
}

pub(super) async fn stored_response(
    State(node): State<Arc<Node>>,
    Extension(trace): Extension<Uuid>,
    Extension(streaming): Extension<super::Streaming>,
    axum::extract::Path(handle): axum::extract::Path<String>,
    axum::extract::OriginalUri(uri): axum::extract::OriginalUri,
    method: http::Method,
    headers: HeaderMap,
) -> Response {
    let result = async {
        let raw = Route::Models
            .key(&headers)
            .map_err(|_| Failure(StatusCode::UNAUTHORIZED, "invalid_api_key"))?;
        let index = node.current().ok_or_else(Failure::unavailable)?;
        let key = index.authenticate(raw)?;
        let id = Generate::handle(&handle)
            .ok_or(Failure(StatusCode::NOT_FOUND, "response_not_found"))?;
        let permit = node
            .capacity
            .clone()
            .try_acquire_owned()
            .map_err(|_| Failure::unavailable())?;
        let record = node.db.resolve_response(id, key.id, index.revision).await?;
        let dep = index
            .deployment(record.deployment)
            .ok_or_else(Failure::unavailable)?;
        let secret = credential(&node, &index, dep).await?;
        let mut url = crate::upstream::Upstream::endpoint(
            &dep.base_url,
            Endpoint::OpenaiResponses,
            Operation::Generate,
        );
        url.path_segments_mut()
            .map_err(|_| Failure::unavailable())?
            .push(&record.provider_id);
        let inputs = uri.path().ends_with("/input_items");
        if inputs {
            url.path_segments_mut()
                .map_err(|_| Failure::unavailable())?
                .push("input_items");
        }
        let mut stream = None;
        let mut cursor = None;
        if let Some(query) = uri.query() {
            for (k, v) in url::form_urlencoded::parse(query.as_bytes()) {
                match k.as_ref() {
                    "stream" if !inputs && method == http::Method::GET && stream.is_none() => {
                        stream = Some(v.parse::<bool>().map_err(|_| {
                            Failure(StatusCode::BAD_REQUEST, "invalid_response_query")
                        })?);
                    }
                    "starting_after" if !inputs && cursor.is_none() => {
                        cursor =
                            Some(v.parse::<i64>().ok().filter(|&n| n >= -1).ok_or(Failure(
                                StatusCode::BAD_REQUEST,
                                "invalid_response_query",
                            ))?);
                    }
                    "include_obfuscation"
                        if !inputs
                            && method == http::Method::GET
                            && (v == "true" || v == "false") => {}
                    "include" | "include[]" => {}
                    "after" | "limit" | "order" if inputs => {}
                    _ => return Err(Failure(StatusCode::BAD_REQUEST, "invalid_response_query")),
                }
                url.query_pairs_mut().append_pair(&k, &v);
            }
        }
        let stream = stream.unwrap_or(false);
        if cursor.is_some() && !stream {
            return Err(Failure(StatusCode::BAD_REQUEST, "invalid_response_query"));
        }
        let delete = method == http::Method::DELETE;
        if delete {
            node.db.can_delete(id, key.id, index.revision).await?;
        }
        if method == http::Method::POST {
            url.path_segments_mut()
                .map_err(|_| Failure::unavailable())?
                .push("cancel");
        }
        let timeout = std::time::Duration::from_secs(30);
        let call = node
            .upstream
            .client
            .request(method, url)
            .bearer_auth(secret.as_str());
        let call = if stream {
            call.header(http::header::ACCEPT, "text/event-stream")
        } else {
            call.timeout(timeout)
        };
        let response = tokio::time::timeout(timeout, call.send())
            .await
            .map_err(|_| Failure::upstream())?
            .map_err(|_| Failure::upstream())?;
        let status = response.status();
        let interpreter = if status.is_success() && !inputs && !delete {
            node.db
                .stored_interpreter(id, key.id, index.revision)
                .await?
        } else {
            None
        };
        let binding = super::background::ReadBinding {
            interpreter,
            id: handle,
            record,
            key: key.id,
            revision: index.revision,
        };
        let record = &binding.record;
        if stream && status.is_success() {
            if !response
                .headers()
                .get(http::header::CONTENT_TYPE)
                .and_then(|v| v.to_str().ok())
                .is_some_and(|v| v.split(';').next() == Some("text/event-stream"))
            {
                return Err(Failure::upstream());
            }
            streaming.0.cancel();
            return Ok(super::background::resume(
                node.clone(),
                binding,
                response,
                cursor,
                permit,
            ));
        }
        let bytes = read_body(response, node.upstream.response_limit())
            .await
            .map_err(|_| Failure::upstream())?;
        let mut value: Value = serde_json::from_slice(&bytes).map_err(|_| Failure::upstream())?;
        if status.is_success() && inputs {
            if value["object"] != "list" || !value["data"].is_array() {
                return Err(Failure::upstream());
            }
            super::files::public_items(
                &node,
                &index,
                key.id,
                record.model,
                record.deployment,
                &mut value,
            )
            .await?;
        } else if status.is_success() {
            if value["id"] != record.provider_id
                || (delete && value["deleted"] != true)
                || (!delete && value["object"] != "response")
            {
                return Err(Failure::upstream());
            }
            binding
                .rewrite(&node, &mut value)
                .await
                .map_err(|_| Failure::upstream())?;
            if delete {
                node.db.forget_response(id, key.id, index.revision).await?;
            }
        } else if status == StatusCode::NOT_FOUND && delete {
            node.db.forget_response(id, key.id, index.revision).await?;
        }
        if status.is_success() {
            crate::control::resources::ResourceScope::new(
                &node.db,
                key.id,
                index.revision,
                record.model,
                record.deployment,
            )
            .interpreter(interpreter)
            .rewrite(&mut value)
            .await
            .map_err(|_| Failure::upstream())?;
        }
        Ok::<_, Failure>((status, Json(value)).into_response())
    }
    .await;
    result.unwrap_or_else(|e| error(trace, Endpoint::OpenaiResponses, e))
}
