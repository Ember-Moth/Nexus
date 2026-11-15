//! Conversations and their items stay at the provider. Nexus persists the credential and tenant binding.
use super::{BodyLimit, Node, error};
use crate::{
    control::snapshot::Index,
    protocol::{
        Endpoint,
        request::{ConversationId, Generate, Operation, Route},
    },
    request::{Failure, lifecycle::credential},
    upstream::{Upstream, read_body},
};
use axum::{
    Extension, Json,
    body::to_bytes,
    extract::{Request, State},
    response::{IntoResponse, Response},
};
use http::{Method, StatusCode};
use serde_json::{Value, json};
use std::{sync::Arc, time::Duration};
use uuid::Uuid;

const EP: Endpoint = Endpoint::OpenaiResponses;
fn invalid() -> Failure {
    Failure(StatusCode::BAD_REQUEST, "invalid_conversation_request")
}
fn missing() -> Failure {
    Failure(StatusCode::NOT_FOUND, "conversation_not_found")
}
fn label(s: &str) -> bool {
    !s.is_empty()
        && s.len() <= 200
        && s.bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"-_".contains(&b))
}

pub(super) async fn bind(
    node: &Node,
    index: &Index,
    key: &str,
    req: &mut Generate,
) -> Result<(), Failure> {
    if req.endpoint == EP
        && let Some(handle) = req.conversation_handle()
    {
        let key = index.authenticate(key)?;
        let record = node.db.conversation(handle, key.id, index.revision).await?;
        req.bind_deployment(record.model, record.deployment)
            .map_err(|_| invalid())?;
        req.bind_conversation(ConversationId {
            public: format!("conv_nx_{handle}"),
            provider: record.provider_id,
        });
    }
    Ok(())
}

// Only explicit items can enter an unmetered resource write. Provider-side references are
// accepted via generation after checking the referenced item belongs to the parent.
fn items(value: &Value) -> Result<(), Failure> {
    let list = value
        .as_array()
        .filter(|v| v.len() <= 20)
        .ok_or_else(invalid)?;
    for item in list {
        if !item.is_object() || item.get("id").is_some() {
            return Err(invalid());
        }
        match item["type"].as_str() {
            None | Some("message")
                if matches!(
                    item["role"].as_str(),
                    Some("system" | "developer" | "user" | "assistant")
                ) =>
            {
                if let Some(parts) = item["content"].as_array() {
                    for part in parts {
                        if !matches!(
                            part["type"].as_str(),
                            Some(
                                "input_text"
                                    | "output_text"
                                    | "input_image"
                                    | "input_file"
                                    | "refusal"
                            )
                        ) {
                            return Err(invalid());
                        }
                    }
                } else if !item["content"].is_string() {
                    return Err(invalid());
                }
            }
            Some("function_call" | "custom_tool_call") => {}
            Some("function_call_output" | "custom_tool_call_output")
                if item["output"].is_string() => {}
            _ => return Err(invalid()),
        }
    }
    Ok(())
}

pub(super) async fn handle(
    State(node): State<Arc<Node>>,
    Extension(trace): Extension<Uuid>,
    Extension(BodyLimit(limit)): Extension<BodyLimit>,
    request: Request,
) -> Response {
    operation(&node, trace, limit, request)
        .await
        .unwrap_or_else(|e| error(trace, EP, e))
}

async fn operation(
    node: &Node,
    trace: Uuid,
    limit: usize,
    request: Request,
) -> Result<Response, Failure> {
    let (parts, body) = request.into_parts();
    let key = Route::Models
        .key(&parts.headers)
        .map_err(|_| Failure(StatusCode::UNAUTHORIZED, "invalid_api_key"))?;
    let index = node.current().ok_or_else(Failure::unavailable)?;
    let kid = index.authenticate(key)?.id;
    let _permit = node
        .capacity
        .try_acquire()
        .map_err(|_| Failure::unavailable())?;
    let suffix = parts
        .uri
        .path()
        .strip_prefix("/v1/conversations")
        .ok_or_else(missing)?;
    let path: Vec<&str> = suffix.split('/').filter(|s| !s.is_empty()).collect();
    let creation = path.is_empty();
    let write = parts.method == Method::POST;
    let delete = parts.method == Method::DELETE;
    let collection = path.get(1) == Some(&"items");
    if path.get(2).is_some_and(|s| !label(s)) {
        return Err(invalid());
    }
    let mut file_target = None;
    let payload = if write {
        let bytes = to_bytes(body, limit)
            .await
            .map_err(|_| Failure(StatusCode::PAYLOAD_TOO_LARGE, "invalid_request_body"))?;
        let mut value: Value = if creation && bytes.is_empty() {
            json!({})
        } else {
            serde_json::from_slice(&bytes).map_err(|_| invalid())?
        };
        let obj = value.as_object().ok_or_else(invalid)?;
        if obj.keys().any(|k| !match k.as_str() {
            "items" => creation || collection,
            "metadata" => !collection,
            _ => false,
        }) {
            return Err(invalid());
        }
        if let Some(input) = value.get_mut("items") {
            items(input)?;
            file_target = super::files::bind_items(node, &index, kid, input).await?;
        } else if collection {
            return Err(invalid());
        }
        Some(value)
    } else {
        None
    };
    let (handle, pid, model, dep) = if creation {
        let names = match parts.headers.get("x-nexus-model") {
            Some(v) => vec![v.to_str().map_err(|_| invalid())?],
            None => index.model_names(),
        };
        let mut target = None;
        for name in names {
            let mut req =
                Generate::parse_compact(json!({"model":name,"input":[]})).map_err(|_| invalid())?;
            if let Some((model, dep)) = file_target {
                req.bind_deployment(model, dep).map_err(|_| invalid())?;
            }
            if let Ok(s) = index.select(key, &req) {
                target = Some((s.model, s.deployment));
                break;
            }
        }
        let (model, dep) = target.ok_or(Failure(
            StatusCode::BAD_REQUEST,
            "no_conversation_deployment",
        ))?;
        node.db
            .preflight(kid, index.revision, Some(model), Some(dep.id))
            .await?;
        (trace, None, model, dep)
    } else {
        let handle = ConversationId::handle(path[0]).ok_or_else(missing)?;
        let record = node
            .db
            .conversation(handle, kid, index.revision)
            .await
            .map_err(|e| {
                if e.database_message() == Some("resource_not_found") {
                    missing()
                } else {
                    e.into()
                }
            })?;
        (
            handle,
            Some(record.provider_id),
            record.model,
            index
                .deployment(record.deployment)
                .ok_or_else(Failure::unavailable)?,
        )
    };
    if file_target.is_some_and(|b| b != (model, dep.id)) {
        return Err(Failure(StatusCode::BAD_REQUEST, "file_deployment_mismatch"));
    }
    let secret = credential(node, &index, dep).await?;
    let mut url = Upstream::endpoint(&dep.base_url, EP, Operation::Generate);
    {
        let mut seg = url
            .path_segments_mut()
            .map_err(|_| Failure::unavailable())?;
        seg.pop().push("conversations");
        if let Some(pid) = &pid {
            seg.push(pid).extend(path[1..].iter().copied());
        }
    }
    if let Some(query) = parts.uri.query() {
        if !collection {
            return Err(invalid());
        }
        for (k, v) in url::form_urlencoded::parse(query.as_bytes()) {
            if !matches!(
                k.as_ref(),
                "limit" | "order" | "after" | "include" | "include[]"
            ) {
                return Err(invalid());
            }
            url.query_pairs_mut().append_pair(&k, &v);
        }
    }
    let mut call = node
        .upstream
        .client
        .request(parts.method, url)
        .bearer_auth(secret.as_str())
        .timeout(Duration::from_secs(60));
    if let Some(payload) = payload {
        call = call.json(&payload);
    }
    let reply = call.send().await.map_err(|_| Failure::upstream())?;
    let status = reply.status();
    let bytes = read_body(reply, node.upstream.response_limit())
        .await
        .map_err(|_| Failure::upstream())?;
    let mut value: Value = serde_json::from_slice(&bytes).map_err(|_| Failure::upstream())?;
    if status.is_success() && (!collection || value["object"] == "conversation") {
        let returned = value["id"]
            .as_str()
            .filter(|id| label(id))
            .ok_or_else(Failure::upstream)?;
        if pid.as_deref().is_some_and(|p| p != returned) {
            return Err(Failure::upstream());
        }
        if delete && !collection {
            if value["deleted"] != true || value["object"] != "conversation.deleted" {
                return Err(Failure::upstream());
            }
            node.db
                .remove_conversation(handle, kid, index.revision)
                .await?;
        } else if value["object"] != "conversation" {
            return Err(Failure::upstream());
        } else if creation {
            let save = || {
                node.db
                    .save_conversation(handle, kid, index.revision, model, dep.id, returned)
            };
            if let Err(e) = save().await {
                if !e.retryable() {
                    return Err(e.into());
                }
                save().await?;
            }
        }
        value["id"] = format!("conv_nx_{handle}").into();
    } else if delete && !collection && status == StatusCode::NOT_FOUND {
        node.db
            .remove_conversation(handle, kid, index.revision)
            .await?;
    }
    if status.is_success() && collection && value["object"] != "conversation" {
        super::files::public_items(node, &index, kid, model, dep.id, &mut value).await?;
        crate::control::resources::ResourceScope::new(&node.db, kid, index.revision, model, dep.id)
            .rewrite(&mut value)
            .await
            .map_err(|_| Failure::upstream())?;
    }
    Ok((status, Json(value)).into_response())
}
