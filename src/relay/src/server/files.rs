//! Owned file handles are rewritten before the provider sees them. Bytes remain with the provider.
use super::{BodyLimit, Node, error};
use crate::{
    control::{
        database::FilePage,
        snapshot::{Index, Rejection},
    },
    protocol::features::Feature,
    protocol::{
        Endpoint,
        request::{AnthropicHeaders, Generate, Operation, Route},
    },
    request::{Failure, lifecycle::credential},
    upstream::{Upstream, read_body},
};
use axum::{
    Extension, Json,
    body::{Body, to_bytes},
    extract::{Path, Request, State},
    response::{IntoResponse, Response},
};
use http::{HeaderMap, Method, StatusCode, header};
use serde_json::{Value, json};
use std::{collections::HashMap, sync::Arc, time::Duration};
use uuid::Uuid;

fn endpoint(headers: &HeaderMap) -> Endpoint {
    if headers.contains_key(header::AUTHORIZATION) && !headers.contains_key("x-api-key") {
        Endpoint::OpenaiResponses
    } else {
        Endpoint::AnthropicMessages
    }
}

fn handle(value: &str) -> Result<Uuid, Failure> {
    value
        .strip_prefix("file_nx_")
        .and_then(|s| s.parse().ok())
        .ok_or(Failure(StatusCode::NOT_FOUND, "file_not_found"))
}
pub(super) fn resource_error(e: crate::control::ControlError) -> Failure {
    if e.database_message() == Some("resource_not_found") {
        Failure(StatusCode::NOT_FOUND, "file_not_found")
    } else {
        e.into()
    }
}

pub(super) async fn bind(
    node: &Node,
    index: &Index,
    raw: &str,
    request: &mut Generate,
) -> Result<(), Failure> {
    if let Some(id) = request.execution.as_ref().and_then(|e| e.container) {
        let kid = index.authenticate(raw)?.id;
        let c = node
            .db
            .container(id, kid, index.revision, request.endpoint)
            .await
            .map_err(resource_error)?;
        request
            .bind_deployment(c.model, c.deployment)
            .map_err(|_| Failure(StatusCode::BAD_REQUEST, "container_deployment_mismatch"))?;
        request.set_container(c.provider_id);
    }
    if request.endpoint == Endpoint::AnthropicMessages {
        let mut citations = Vec::new();
        for (i, m) in request.body()["messages"]
            .as_array()
            .into_iter()
            .flatten()
            .enumerate()
        {
            for path in crate::protocol::history::citations::claude_files(m) {
                let fid = m.pointer(&path).ok_or_else(Failure::unavailable)?;
                let id = crate::protocol::history::citations::file_handle(fid, "file_nx_")
                    .map_err(|_| Failure(StatusCode::BAD_REQUEST, "invalid_file_reference"))?;
                citations.push((format!("/messages/{i}{path}"), id));
            }
        }
        if !citations.is_empty() {
            request.features.insert(Feature::Files);
            let kid = index.authenticate(raw)?.id;
            let (binding, fields) = resolved(
                node,
                index,
                kid,
                Endpoint::AnthropicMessages,
                citations,
                false,
            )
            .await?;
            if let Some((model, dep)) = binding {
                request
                    .bind_deployment(model, dep)
                    .map_err(|_| Failure(StatusCode::BAD_REQUEST, "file_deployment_mismatch"))?;
            }
            for (path, _) in fields {
                request.strip_citation_file(&path);
            }
        }
    }
    let refs = request
        .file_handles()
        .map_err(|_| Failure(StatusCode::BAD_REQUEST, "invalid_file_reference"))?;
    if refs.is_empty() {
        return Ok(());
    }
    request.features.insert(Feature::Files);
    let key = index.authenticate(raw)?;
    let (binding, fields) = resolved(node, index, key.id, request.endpoint, refs, true).await?;
    if let Some((model, dep)) = binding {
        request
            .bind_deployment(model, dep)
            .map_err(|_| Failure(StatusCode::BAD_REQUEST, "file_deployment_mismatch"))?;
    }
    for (path, pid) in fields {
        request.set_file(&path, pid);
    }
    Ok(())
}

async fn resolved(
    node: &Node,
    index: &Index,
    kid: Uuid,
    ep: Endpoint,
    refs: Vec<(String, Uuid)>,
    cross: bool,
) -> Result<(Option<(Uuid, Uuid)>, Vec<(String, String)>), Failure> {
    let mut seen = HashMap::new();
    let mut binding = None;
    let mut fields = Vec::with_capacity(refs.len());
    for (path, id) in refs {
        if !seen.contains_key(&id) {
            if seen.len() >= 64 {
                return Err(Failure(StatusCode::BAD_REQUEST, "too_many_files"));
            }
            let file = match node.db.native_file(id, kid, index.revision, ep).await {
                Err(e) if cross && e.database_message() == Some("resource_not_found") => {
                    let other = match ep {
                        Endpoint::OpenaiResponses => Endpoint::AnthropicMessages,
                        Endpoint::AnthropicMessages => Endpoint::OpenaiResponses,
                    };
                    node.db.native_file(id, kid, index.revision, other).await
                }
                result => result,
            }
            .map_err(resource_error)?;
            let target = (file.model, file.deployment);
            if binding.is_some_and(|b| b != target) {
                return Err(Failure(StatusCode::BAD_REQUEST, "file_deployment_mismatch"));
            }
            binding = Some(target);
            seen.insert(id, file.provider_id);
        }
        fields.push((path, seen[&id].clone()));
    }
    Ok((binding, fields))
}

pub(super) async fn bind_items(
    node: &Node,
    index: &Index,
    kid: Uuid,
    items: &mut Value,
) -> Result<Option<(Uuid, Uuid)>, Failure> {
    if items
        .as_array()
        .is_some_and(|items| items.iter().any(crate::protocol::history::reserved))
    {
        return Err(Failure(
            StatusCode::BAD_REQUEST,
            "private_history_requires_generation",
        ));
    }
    let refs = Generate::input_files(items)
        .map_err(|_| Failure(StatusCode::BAD_REQUEST, "invalid_file_reference"))?;
    let (binding, fields) =
        resolved(node, index, kid, Endpoint::OpenaiResponses, refs, false).await?;
    for (path, pid) in fields {
        *items.pointer_mut(&path).expect("validated file path") = pid.into();
    }
    Ok(binding)
}

pub(super) async fn public_items(
    node: &Node,
    index: &Index,
    kid: Uuid,
    model: Uuid,
    dep: Uuid,
    value: &mut Value,
) -> Result<(), Failure> {
    let mut fields = Vec::new();
    if let Some(items) = value["data"].as_array() {
        for (i, item) in items.iter().enumerate() {
            fields.extend(
                Generate::item_file_fields(item, &format!("/data/{i}"))
                    .map_err(|_| Failure::upstream())?,
            );
        }
    } else {
        fields = Generate::item_file_fields(value, "").map_err(|_| Failure::upstream())?;
    }
    let mut handles = HashMap::new();
    let mut updates = Vec::new();
    for (path, pid) in fields {
        if !handles.contains_key(pid) {
            let local = node
                .db
                .openai_file_id(kid, index.revision, model, dep, pid)
                .await?
                .ok_or_else(Failure::upstream)?;
            handles.insert(pid, local);
        }
        updates.push((path, handles[pid]));
    }
    for (path, local) in updates {
        *value.pointer_mut(&path).expect("validated file path") = format!("file_nx_{local}").into();
    }
    Ok(())
}

pub(super) async fn collection(
    State(node): State<Arc<Node>>,
    Extension(trace): Extension<Uuid>,
    Extension(BodyLimit(limit)): Extension<BodyLimit>,
    req: Request,
) -> Response {
    let ep = endpoint(req.headers());
    request(&node, None, limit, req, ep)
        .await
        .unwrap_or_else(|e| error(trace, ep, e))
}
pub(super) async fn item(
    State(node): State<Arc<Node>>,
    Extension(trace): Extension<Uuid>,
    Path(id): Path<String>,
    req: Request,
) -> Response {
    let ep = endpoint(req.headers());
    request(&node, Some(&id), 0, req, ep)
        .await
        .unwrap_or_else(|e| error(trace, ep, e))
}

fn files_headers(headers: &HeaderMap) -> Result<AnthropicHeaders, Failure> {
    let mut headers = headers.clone();
    if !headers.get_all("anthropic-beta").iter().any(|v| {
        v.to_str()
            .is_ok_and(|v| v.split(',').any(|s| s.trim() == "files-api-2025-04-14"))
    }) {
        headers.append(
            "anthropic-beta",
            http::HeaderValue::from_static("files-api-2025-04-14"),
        );
    }
    AnthropicHeaders::parse(&headers)
        .map_err(|_| Failure(StatusCode::BAD_REQUEST, "invalid_anthropic_headers"))
}

async fn hydrate_file(
    node: &Node,
    index: &Index,
    kid: Uuid,
    id: Uuid,
    headers: &AnthropicHeaders,
) -> Result<(), Failure> {
    let record = node
        .db
        .file(id, kid, index.revision)
        .await
        .map_err(resource_error)?;
    let dep = index
        .deployment(record.deployment)
        .ok_or_else(Failure::unavailable)?;
    let token = credential(node, index, dep).await?;
    let mut url = Upstream::endpoint(
        &dep.base_url,
        Endpoint::AnthropicMessages,
        Operation::Generate,
    );
    url.path_segments_mut()
        .map_err(|_| Failure::unavailable())?
        .pop()
        .push("files")
        .push(&record.provider_id);
    let call = headers.apply(
        node.upstream
            .client
            .get(url)
            .header("x-api-key", token.as_str()),
    );
    let reply = call
        .timeout(Duration::from_secs(30))
        .send()
        .await
        .map_err(|_| Failure::upstream())?;
    if reply.status() == StatusCode::NOT_FOUND {
        node.db.remove_file(id, kid, index.revision).await?;
        return Ok(());
    }
    if !reply.status().is_success() {
        return Err(Failure::upstream());
    }
    let bytes = read_body(reply, node.upstream.response_limit())
        .await
        .map_err(|_| Failure::upstream())?;
    let meta: Value = serde_json::from_slice(&bytes).map_err(|_| Failure::upstream())?;
    if meta["id"] != record.provider_id {
        return Err(Failure::upstream());
    }
    node.db
        .refresh_execution_file(id, kid, index.revision, &meta)
        .await?;
    Ok(())
}

async fn request(
    node: &Node,
    file: Option<&str>,
    limit: usize,
    req: Request,
    ep: Endpoint,
) -> Result<Response, Failure> {
    let (parts, body) = req.into_parts();
    let key_text = Route::Generate(ep)
        .key(&parts.headers)
        .map_err(|_| Failure(StatusCode::UNAUTHORIZED, "invalid_api_key"))?;
    let index = node.current().ok_or_else(Failure::unavailable)?;
    let key = index.authenticate(key_text)?;
    let _admitted = super::tenancy::admit(node, &index, key).await?;
    let messages = ep == Endpoint::AnthropicMessages;
    if file.is_none() && parts.method == Method::GET {
        let mut page = FilePage {
            limit: if messages { 20 } else { 10000 },
            cursor: None,
            newer: false,
            ascending: false,
            purpose: None,
        };
        for (k, v) in url::form_urlencoded::parse(parts.uri.query().unwrap_or("").as_bytes()) {
            match k.as_ref() {
                "limit" => {
                    page.limit = v
                        .parse::<i32>()
                        .ok()
                        .filter(|n| (1..=if messages { 1000 } else { 10000 }).contains(n))
                        .ok_or(Failure(StatusCode::BAD_REQUEST, "invalid_limit"))?
                }
                "before_id" | "after_id" if messages && page.cursor.is_none() => {
                    page.cursor = Some(handle(&v)?);
                    page.newer = k == "before_id";
                }
                "after" if !messages && page.cursor.is_none() => {
                    page.cursor = Some(handle(&v)?);
                }
                "order" if !messages && (v == "asc" || v == "desc") => page.ascending = v == "asc",
                "purpose" if !messages => page.purpose = Some(v.into_owned()),
                _ => return Err(Failure(StatusCode::BAD_REQUEST, "invalid_file_query")),
            }
        }
        if !messages {
            page.newer = page.ascending;
        }
        let limit = page.limit;
        page.limit += 1;
        let headers = if messages {
            Some(files_headers(&parts.headers)?)
        } else {
            None
        };
        let (rows, more) = loop {
            let mut rows = node
                .db
                .native_files(key.id, index.revision, ep, page.clone())
                .await
                .map_err(resource_error)?;
            let more = rows.len() > limit as usize;
            rows.truncate(limit as usize);
            let Some(headers) = &headers else {
                break (rows, more);
            };
            let missing = rows
                .iter()
                .filter(|r| r.get("filename").is_none())
                .map(|row| handle(row["id"].as_str().ok_or_else(Failure::upstream)?))
                .collect::<Result<Vec<_>, _>>()?;
            if missing.is_empty() {
                break (rows, more);
            }
            use futures_util::{StreamExt, TryStreamExt};
            futures_util::stream::iter(missing)
                .map(|id| hydrate_file(node, &index, key.id, id, headers))
                .buffer_unordered(8)
                .try_collect::<Vec<_>>()
                .await?;
        };
        let mut listing = json!({"has_more":more,"first_id":rows.first().map(|v|&v["id"]),"last_id":rows.last().map(|v|&v["id"]),"data":rows});
        if !messages {
            listing["object"] = "list".into();
        }
        return Ok(Json(listing).into_response());
    }
    // Read an upload before any other refusal, so the client receives the answer instead of
    // a reset; a slow sender cannot hold the slot past the upload deadline.
    let upload = if parts.method == Method::POST {
        if !parts.headers.get(header::CONTENT_TYPE).is_some_and(|v| {
            v.to_str()
                .is_ok_and(|s| s.starts_with("multipart/form-data;"))
        }) {
            return Err(Failure(StatusCode::BAD_REQUEST, "multipart_required"));
        }
        Some(
            tokio::time::timeout(super::UPLOAD_DEADLINE, to_bytes(body, limit))
                .await
                .map_err(|_| Failure(StatusCode::REQUEST_TIMEOUT, "upload_timeout"))?
                .map_err(|_| Failure(StatusCode::PAYLOAD_TOO_LARGE, "file_too_large"))?,
        )
    } else {
        None
    };
    let mut path = Vec::new();
    let (id, model, dep) = if let Some(file) = file {
        let id = handle(file)?;
        let data = node
            .db
            .native_file(id, key.id, index.revision, ep)
            .await
            .map_err(resource_error)?;
        path.push(data.provider_id);
        if parts.uri.path().ends_with("/content") {
            path.push("content".into());
        }
        (
            id,
            data.model,
            index
                .deployment(data.deployment)
                .ok_or_else(Failure::unavailable)?,
        )
    } else {
        let names = if let Some(value) = parts.headers.get("x-nexus-model") {
            vec![
                value
                    .to_str()
                    .map_err(|_| Failure(StatusCode::BAD_REQUEST, "invalid_model"))?,
            ]
        } else {
            index.model_names()
        };
        let mut target = None;
        let mut disabled = false;
        for name in names {
            let selection = if messages {
                let mut req = Generate::parse_count(
                    json!({"model":name,"messages":[{"role":"user","content":"file"}]}),
                )
                .map_err(|_| Failure::unavailable())?;
                req.features.insert(Feature::Files);
                index.native_messages(key_text, &req, None)
            } else {
                let mut req = Generate::parse_compact(json!({"model":name,"input":[]}))
                    .map_err(|_| Failure::unavailable())?;
                req.features.insert(Feature::Files);
                index.select(key_text, &req)
            };
            match selection {
                Ok(value) => {
                    target = Some((value.model, value.deployment));
                    break;
                }
                Err(e) => disabled |= matches!(e, Rejection::FeatureDisabled),
            }
        }
        let (model, dep) = target.ok_or(if disabled {
            Failure::feature_disabled()
        } else {
            Failure(StatusCode::BAD_REQUEST, "no_file_deployment")
        })?;
        node.db
            .preflight(key.id, index.revision, Some(model), Some(dep.id))
            .await?;
        super::tenancy::room(node, &index, key, "file").await?;
        (Uuid::new_v4(), model, dep)
    };
    let token = credential(node, &index, dep).await?;
    let mut url = Upstream::endpoint(&dep.base_url, ep, Operation::Generate);
    {
        let mut segments = url
            .path_segments_mut()
            .map_err(|_| Failure::unavailable())?;
        segments.pop().push("files");
        for p in &path {
            segments.push(p);
        }
    }
    let call = node
        .upstream
        .client
        .request(parts.method.clone(), url)
        .timeout(Duration::from_secs(90));
    let mut call = if messages {
        files_headers(&parts.headers)?.apply(call.header("x-api-key", token.as_str()))
    } else {
        call.bearer_auth(token.as_str())
    };
    if let Some(upload) = upload {
        let ct = parts
            .headers
            .get(header::CONTENT_TYPE)
            .ok_or(Failure(StatusCode::BAD_REQUEST, "multipart_required"))?;
        call = call.header(header::CONTENT_TYPE, ct).body(upload);
    }
    let upstream = call.send().await.map_err(|_| Failure::upstream())?;
    let status = upstream.status();
    let media = upstream.headers().get(header::CONTENT_TYPE).cloned();
    let bytes = read_body(upstream, node.upstream.response_limit())
        .await
        .map_err(|_| Failure::upstream())?;
    if status.is_success() && path.len() == 2 {
        let mut res = Response::new(Body::from(bytes));
        if let Some(media) = media {
            res.headers_mut().insert(header::CONTENT_TYPE, media);
        }
        return Ok(res);
    }
    let mut value: Value = serde_json::from_slice(&bytes).map_err(|_| Failure::upstream())?;
    if status.is_success() {
        let pid = value["id"]
            .as_str()
            .filter(|v| {
                v.len() <= 200
                    && !v.is_empty()
                    && v.bytes()
                        .all(|c| c.is_ascii_alphanumeric() || matches!(c, b'_' | b'-'))
            })
            .ok_or_else(Failure::upstream)?;
        if file.is_none() {
            if value[if messages { "type" } else { "object" }] != "file" {
                return Err(Failure::upstream());
            }
            let save = || {
                node.db
                    .save_native_file(id, key.id, index.revision, (model, dep.id), &value, ep)
            };
            let saved = match save().await {
                Err(e) if e.retryable() => save().await,
                saved => saved,
            };
            if let Err(e) = saved {
                // The provider holds a file no handle refers to.
                super::purge::orphaned(node, &index, dep, key.id, "file", ep, pid).await;
                return Err(e.into());
            }
        } else {
            if path[0] != pid {
                return Err(Failure::upstream());
            }
            if parts.method == Method::DELETE {
                if (!messages && (value["deleted"] != true || value["object"] != "file"))
                    || (messages && value["type"] != "file_deleted")
                {
                    return Err(Failure::upstream());
                }
                node.db
                    .remove_native_file(id, key.id, index.revision, ep)
                    .await?;
            }
        }
        value["id"] = format!("file_nx_{id}").into();
    } else if status == StatusCode::NOT_FOUND && parts.method == Method::DELETE {
        node.db
            .remove_native_file(id, key.id, index.revision, ep)
            .await?;
    }
    Ok(super::reply(status, value, ep))
}
