//! Native OpenAI vector stores, file ingestion and direct retrieval, scoped to one enterprise key.
use super::{BodyLimit, Node, error};
use crate::{
    control::{
        database::Gateway,
        snapshot::{Deployment, Index, Rejection},
        vectors::Observation,
    },
    protocol::{
        Endpoint,
        features::Feature,
        request::{Generate, Operation, Route},
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
use std::{collections::HashMap, sync::Arc, time::Duration};
use tokio_util::sync::CancellationToken;
use uuid::Uuid;

const EP: Endpoint = Endpoint::OpenaiResponses;
const ROOT: &str = "/v1/vector_stores";
fn invalid() -> Failure {
    Failure(StatusCode::BAD_REQUEST, "invalid_vector_store_request")
}
fn absent() -> Failure {
    Failure(StatusCode::NOT_FOUND, "vector_store_not_found")
}
fn handle(s: &str, prefix: &str) -> Result<Uuid, Failure> {
    s.strip_prefix(prefix)
        .and_then(|v| v.parse().ok())
        .ok_or_else(absent)
}
fn label(v: &Value) -> Result<&str, Failure> {
    v.as_str()
        .filter(|s| {
            !s.is_empty()
                && s.len() <= 200
                && s.bytes()
                    .all(|b| b.is_ascii_alphanumeric() || b"-_".contains(&b))
        })
        .ok_or_else(Failure::upstream)
}
fn norm(e: crate::control::ControlError) -> Failure {
    if e.database_message() == Some("resource_not_found") {
        absent()
    } else {
        e.into()
    }
}
fn public(id: Uuid) -> String {
    format!("vs_nx_{id}")
}

pub(super) async fn bind(
    node: &Node,
    index: &Index,
    raw: &str,
    request: &mut Generate,
) -> Result<(), Failure> {
    let Some(spec) = &request.file_search else {
        return Ok(());
    };
    let key = index.authenticate(raw)?;
    let mut providers = Vec::with_capacity(spec.stores.len());
    for id in spec.stores.clone() {
        let v = node
            .db
            .vector(id, key.id, index.revision)
            .await
            .map_err(norm)?;
        if v.state != "active" {
            return Err(Failure(StatusCode::BAD_REQUEST, "vector_store_expired"));
        }
        request
            .bind_deployment(v.model_id, v.deployment_id)
            .map_err(|_| invalid())?;
        providers.push(v.provider_id.ok_or_else(absent)?);
    }
    request.set_vector_stores(providers);
    Ok(())
}

fn view(id: Uuid, v: &Value) -> Result<(Value, &'static str), Failure> {
    label(&v["id"])?;
    if v["created_at"]
        .as_i64()
        .filter(|n| *n >= 0)
        .and_then(|n| chrono::DateTime::from_timestamp(n, 0))
        .is_none()
        || v.get("expires_at")
            .filter(|t| !t.is_null())
            .is_some_and(|t| {
                t.as_i64()
                    .is_none_or(|n| n < 0 || chrono::DateTime::from_timestamp(n, 0).is_none())
            })
        || v["object"] != "vector_store"
        || v["usage_bytes"].as_i64().is_none_or(|n| n < 0)
        || !v["file_counts"].is_object()
    {
        return Err(Failure::upstream());
    }
    let state = match v["status"].as_str() {
        Some("completed" | "in_progress") => "active",
        Some("expired") => "expired",
        _ => return Err(Failure::upstream()),
    };
    let mut obj = json!({"id":public(id),"object":"vector_store"});
    for k in [
        "name",
        "created_at",
        "usage_bytes",
        "file_counts",
        "status",
        "expires_after",
        "expires_at",
        "last_active_at",
        "metadata",
        "description",
    ] {
        if let Some(field) = v.get(k) {
            obj[k] = field.clone();
        }
    }
    Ok((obj, state))
}

async fn call(
    node: &Node,
    index: &Index,
    dep: &Deployment,
    method: Method,
    path: &[String],
) -> Result<reqwest::RequestBuilder, Failure> {
    let token = credential(node, index, dep).await?;
    let mut url = Upstream::endpoint(&dep.base_url, EP, Operation::Generate);
    url.path_segments_mut()
        .map_err(|_| Failure::unavailable())?
        .pop()
        .push("vector_stores")
        .extend(path);
    Ok(node
        .upstream
        .client
        .request(method, url)
        .bearer_auth(token.as_str())
        .timeout(Duration::from_secs(90)))
}

pub(super) async fn handle_request(
    State(node): State<Arc<Node>>,
    Extension(id): Extension<Uuid>,
    Extension(BodyLimit(limit)): Extension<BodyLimit>,
    req: Request,
) -> Response {
    let (parts, body) = req.into_parts();
    let result = async {
        let raw = Route::Models
            .key(&parts.headers)
            .map_err(|_| Failure(StatusCode::UNAUTHORIZED, "invalid_api_key"))?
            .to_owned();
        let index = node.current().ok_or_else(Failure::unavailable)?;
        let kid = index.authenticate(&raw)?.id;
        let permit = node
            .capacity
            .clone()
            .try_acquire_owned()
            .map_err(|_| Failure::unavailable())?;
        let bytes = to_bytes(body, limit)
            .await
            .map_err(|_| Failure(StatusCode::PAYLOAD_TOO_LARGE, "invalid_request_body"))?;
        let body = if bytes.is_empty() {
            json!({})
        } else {
            serde_json::from_slice(&bytes).map_err(|_| invalid())?
        };
        let req = OperationRequest {
            node: node.clone(),
            index,
            kid,
            raw,
        };
        // Mutations that already reached the supplier must record their outcome even if the client leaves.
        node.tasks
            .spawn(async move {
                let _permit = permit;
                req.run(id, parts, body).await
            })
            .await
            .map_err(|_| Failure::unavailable())?
    }
    .await;
    result.unwrap_or_else(|e| error(id, EP, e))
}

fn upload_fields(path: &[&str], body: &Value) -> Result<Vec<String>, Failure> {
    let allowed: &[&str] = match path {
        [] => &[
            "name",
            "description",
            "metadata",
            "expires_after",
            "file_ids",
            "chunking_strategy",
        ],
        [_] => &["name", "description", "metadata", "expires_after"],
        [_, "files"] => &["file_id", "attributes", "chunking_strategy"],
        [_, "files", _] => &["attributes"],
        [_, "file_batches"] => &["file_ids", "files", "attributes", "chunking_strategy"],
        [_, "file_batches", _, "cancel"] => &[],
        [_, "search"] => &[
            "query",
            "filters",
            "max_num_results",
            "ranking_options",
            "rewrite_query",
        ],
        _ => return Err(invalid()),
    };
    let obj = body.as_object().ok_or_else(invalid)?;
    if obj.keys().any(|k| !allowed.contains(&k.as_str())) {
        return Err(invalid());
    }
    let mut fields = Vec::new();
    if matches!(path, [_, "files"]) {
        fields.push("/file_id".to_owned());
    }
    if let Some(ids) = obj.get("file_ids").filter(|v| !v.is_null()) {
        if body.get("files").is_some_and(|v| !v.is_null()) {
            return Err(invalid());
        }
        for i in 0..ids.as_array().ok_or_else(invalid)?.len() {
            fields.push(format!("/file_ids/{i}"));
        }
    }
    if let Some(items) = obj.get("files").filter(|v| !v.is_null()) {
        for (i, item) in items.as_array().ok_or_else(invalid)?.iter().enumerate() {
            if item.as_object().is_none_or(|o| {
                o.keys()
                    .any(|k| !["file_id", "attributes", "chunking_strategy"].contains(&k.as_str()))
            }) {
                return Err(invalid());
            }
            fields.push(format!("/files/{i}/file_id"));
        }
    }
    Ok(fields)
}

struct Query {
    limit: i32,
    asc: bool,
    cursor: Option<String>,
    before: bool,
    pairs: Vec<(String, String)>,
}
impl Query {
    fn parse(uri: &http::Uri, files: bool) -> Result<Self, Failure> {
        let mut q = Self {
            limit: 20,
            asc: false,
            cursor: None,
            before: false,
            pairs: Vec::new(),
        };
        for (k, v) in url::form_urlencoded::parse(uri.query().unwrap_or("").as_bytes()) {
            match k.as_ref() {
                "limit" => {
                    q.limit = v
                        .parse()
                        .ok()
                        .filter(|n| (1..=100).contains(n))
                        .ok_or_else(invalid)?
                }
                "order" if v == "asc" || v == "desc" => q.asc = v == "asc",
                "after" | "before" if q.cursor.is_none() => {
                    q.before = k == "before";
                    q.cursor = Some(v.to_string());
                }
                "filter" if files => {}
                _ => return Err(invalid()),
            }
            q.pairs.push((k.into_owned(), v.into_owned()));
        }
        Ok(q)
    }
}

struct OperationRequest {
    node: Arc<Node>,
    index: Arc<Index>,
    kid: Uuid,
    raw: String,
}
impl OperationRequest {
    async fn file(&self, id: &str) -> Result<(String, (Uuid, Uuid)), Failure> {
        let f = self
            .node
            .db
            .native_file(handle(id, "file_nx_")?, self.kid, self.index.revision, EP)
            .await
            .map_err(norm)?;
        Ok((f.provider_id, (f.model, f.deployment)))
    }
    async fn run(
        &self,
        trace: Uuid,
        parts: http::request::Parts,
        mut body: Value,
    ) -> Result<Response, Failure> {
        let path: Vec<_> = parts
            .uri
            .path()
            .strip_prefix(ROOT)
            .ok_or_else(absent)?
            .split('/')
            .filter(|s| !s.is_empty())
            .collect();
        let rev = self.index.revision;
        let db = &self.node.db;
        let get = parts.method == Method::GET;
        let delete = parts.method == Method::DELETE;
        let creating = path.is_empty();
        let files = path.get(1) == Some(&"files");
        let batches = path.get(1) == Some(&"file_batches");
        let search = path.get(1) == Some(&"search");
        let list = creating && get;
        if !body.is_object() {
            return Err(invalid());
        }
        let mut query = Query::parse(&parts.uri, files || batches)?;
        if list {
            let cursor = query
                .cursor
                .as_deref()
                .map(|s| handle(s, "vs_nx_"))
                .transpose()?;
            let mut rows = db
                .vector_page(
                    self.kid,
                    rev,
                    cursor,
                    if query.before { !query.asc } else { query.asc },
                    query.limit + 1,
                )
                .await
                .map_err(norm)?;
            let more = rows.len() > query.limit as usize;
            rows.truncate(query.limit as usize);
            if query.before {
                rows.reverse();
            }
            return Ok(Json(json!({"object":"list","first_id":rows.first().map(|v|&v["id"]),"last_id":rows.last().map(|v|&v["id"]),"has_more":more,"data":rows})).into_response());
        }
        let mut bound = None;
        let mut remote = Vec::new();
        let mut known_files = HashMap::new();
        let translate = if parts.method == Method::POST {
            upload_fields(&path, &body)?
        } else {
            Vec::new()
        };
        for field in translate {
            let local = body
                .pointer(&field)
                .and_then(Value::as_str)
                .ok_or_else(invalid)?;
            let (pid, target) = self.file(local).await?;
            if bound.is_some_and(|b| b != target) {
                return Err(invalid());
            }
            bound = Some(target);
            known_files.insert(pid.clone(), local.to_owned());
            *body.pointer_mut(&field).unwrap() = pid.into();
        }
        let (id, dep, model, stamp) = if creating {
            let names = if let Some(v) = parts.headers.get("x-nexus-model") {
                vec![v.to_str().map_err(|_| invalid())?]
            } else {
                self.index.model_names()
            };
            let mut target = None;
            let mut disabled = false;
            for name in names {
                let mut req = Generate::parse_compact(json!({"model":name,"input":[]}))
                    .map_err(|_| invalid())?;
                req.features.insert(Feature::VectorStores);
                if bound.is_some() {
                    req.features.insert(Feature::Files);
                }
                if let Some((model, dep)) = bound {
                    req.bind_deployment(model, dep).map_err(|_| invalid())?;
                }
                match self.index.vector_target(&self.raw, &req) {
                    Ok(s) => {
                        target = Some((s.model, s.deployment));
                        break;
                    }
                    Err(e) => disabled |= matches!(e, Rejection::FeatureDisabled),
                }
            }
            let (model, dep) = target.ok_or(if disabled {
                Failure::feature_disabled()
            } else {
                Failure(StatusCode::BAD_REQUEST, "no_file_search_deployment")
            })?;
            (trace, dep, model, None)
        } else {
            let id = handle(path[0], "vs_nx_")?;
            let store = if delete && path.len() == 1 {
                db.cleanup_vector(id, self.kid, rev).await
            } else {
                db.vector(id, self.kid, rev).await
            }
            .map_err(norm)?;
            if bound.is_some_and(|b| b != (store.model_id, store.deployment_id)) {
                return Err(invalid());
            }
            remote.push(store.provider_id.ok_or_else(absent)?);
            (
                id,
                self.index
                    .deployment(store.deployment_id)
                    .ok_or_else(Failure::unavailable)?,
                store.model_id,
                Some(store.stamp),
            )
        };
        // Reads, deletes and cancellations stay available after the feature is disabled.
        let cancel = path.get(3) == Some(&"cancel");
        if !get && !delete && !cancel && !dep.allows(Feature::VectorStores) {
            return Err(Failure::feature_disabled());
        }
        let mut batch_id = None;
        if files || batches || search {
            remote.push(path[1].to_string());
        }
        if let Some(s) = path.get(2) {
            let provider = if files {
                let (pid, target) = self.file(s).await?;
                if target != (model, dep.id) {
                    return Err(absent());
                }
                known_files.insert(pid.clone(), s.to_string());
                pid
            } else {
                let local = handle(s, "vsfb_nx_")?;
                batch_id = Some(local);
                db.vector_batch(id, self.kid, rev, local, None)
                    .await
                    .map_err(norm)?
            };
            remote.push(provider);
        }
        if let Some(op) = path.get(3) {
            remote.push(op.to_string());
        }
        if files || batches {
            for (k, v) in &mut query.pairs {
                if matches!(k.as_str(), "after" | "before") {
                    let (pid, target) = self.file(v).await?;
                    if target != (model, dep.id) {
                        return Err(absent());
                    }
                    *v = pid;
                }
            }
        }
        if !creating
            && parts.method == Method::POST
            && ((files || batches) && path.len() == 2
                || path.len() == 1 && body.get("expires_after").is_some())
        {
            db.vector_writable(id, self.kid, rev).await.map_err(norm)?;
        }
        let mut request = call(&self.node, &self.index, dep, parts.method.clone(), &remote)
            .await?
            .query(&query.pairs);
        if parts.method == Method::POST {
            request = request.json(&body);
        }
        let stamp = match stamp {
            Some(stamp) => stamp,
            None => {
                db.prepare_vector(trace, self.kid, rev, model, dep.id)
                    .await?
            }
        };
        let exchange = async {
            let response = request.send().await.map_err(|_| Failure::upstream())?;
            let status = response.status();
            let bytes = read_body(response, self.node.upstream.response_limit())
                .await
                .map_err(|_| Failure::upstream())?;
            Ok::<_, Failure>((status, bytes))
        };
        let (status, bytes) = if search {
            super::preflight::with_quota(
                &self.node,
                &self.index,
                self.index.authenticate(&self.raw)?,
                model,
                trace,
                exchange,
            )
            .await?
        } else {
            exchange.await?
        };
        let mut value: Value = serde_json::from_slice(&bytes).map_err(|_| Failure::upstream())?;
        if !status.is_success() {
            if creating || (status == StatusCode::NOT_FOUND && path.len() == 1) {
                let rejected =
                    creating && matches!(status.as_u16(), 400 | 401 | 403 | 404 | 413 | 422 | 429);
                db.observe_vector(Observation {
                    handle: id,
                    key: Some(self.kid),
                    revision: rev,
                    provider: remote.first().map(String::as_str),
                    stamp,
                    metadata: None,
                    state: if rejected {
                        "rejected"
                    } else if creating {
                        "unknown"
                    } else {
                        "deleted"
                    },
                    uncertain: !rejected,
                })
                .await?;
            }
            return Ok(super::reply(status, value, Endpoint::OpenaiResponses));
        }
        if path.len() <= 1 {
            let pid = label(&value["id"])?;
            if remote.first().is_some_and(|s| s != pid) {
                return Err(Failure::upstream());
            }
            if delete {
                if value["object"] != "vector_store.deleted" || value["deleted"] != true {
                    return Err(Failure::upstream());
                }
                db.observe_vector(Observation {
                    handle: id,
                    key: Some(self.kid),
                    revision: rev,
                    provider: Some(pid),
                    stamp,
                    metadata: None,
                    state: "deleted",
                    uncertain: false,
                })
                .await?;
                value["id"] = public(id).into();
            } else {
                let (v, state) = match view(id, &value) {
                    Ok(v) => v,
                    Err(e) => {
                        if creating {
                            db.vector_locator(id, self.kid, pid).await?;
                        }
                        return Err(e);
                    }
                };
                db.observe_vector(Observation {
                    handle: id,
                    key: Some(self.kid),
                    revision: rev,
                    provider: Some(pid),
                    stamp,
                    metadata: Some(&v),
                    state,
                    uncertain: false,
                })
                .await?;
                value = v;
            }
        } else if batches && path.get(3) != Some(&"files") {
            let pid = label(&value["id"])?;
            if value["object"] != "vector_store.files_batch"
                || value["vector_store_id"] != remote[0]
                || remote.get(2).is_some_and(|p| p != pid)
            {
                return Err(Failure::upstream());
            }
            let local = batch_id.unwrap_or(trace);
            if batch_id.is_none() {
                db.vector_batch(id, self.kid, rev, local, Some(pid)).await?;
            }
            value["id"] = format!("vsfb_nx_{local}").into();
            value["vector_store_id"] = public(id).into();
        } else if path.get(3) == Some(&"content") {
            if !value["data"].as_array().is_some_and(|rows| {
                rows.iter()
                    .all(|r| r["type"] == "text" && r["text"].is_string())
            }) {
                return Err(Failure::upstream());
            }
        } else {
            if files
                && parts.method == Method::POST
                && path.len() == 2
                && value["id"] != body["file_id"]
            {
                return Err(Failure::upstream());
            }
            if files && path.len() >= 3 && value["id"] != remote[2] {
                return Err(Failure::upstream());
            }
            if delete
                && (value["object"] != "vector_store.file.deleted" || value["deleted"] != true)
            {
                return Err(Failure::upstream());
            }
            if search
                && (value["object"] != "vector_store.search_results.page"
                    || !value["data"].is_array())
            {
                return Err(Failure::upstream());
            }
            rewrite_files(
                db,
                self.kid,
                rev,
                (model, dep.id),
                (&remote[0], id),
                search,
                &known_files,
                &mut value,
            )
            .await?;
        }
        if !get && (files || batches) {
            db.vector_due(id, self.kid, rev).await?;
        }
        Ok(super::reply(status, value, Endpoint::OpenaiResponses))
    }
}

#[allow(clippy::too_many_arguments)]
async fn rewrite_files(
    db: &Gateway,
    kid: Uuid,
    rev: i64,
    target: (Uuid, Uuid),
    store: (&str, Uuid),
    search: bool,
    known: &HashMap<String, String>,
    body: &mut Value,
) -> Result<(), Failure> {
    let file = async |id: &str| {
        if let Some(local) = known.get(id) {
            return Ok(local.clone());
        }
        let local = db
            .openai_file_id(kid, rev, target.0, target.1, id)
            .await?
            .ok_or_else(Failure::upstream)?;
        Ok::<_, Failure>(format!("file_nx_{local}"))
    };
    let rewrite = async |row: &mut Value| {
        if !search
            && !matches!(
                row["object"].as_str(),
                Some("vector_store.file" | "vector_store.file.deleted")
            )
        {
            return Err(Failure::upstream());
        }
        if row["object"] == "vector_store.file" && row["vector_store_id"] != store.0 {
            return Err(Failure::upstream());
        }
        if let Some(v) = row.get_mut("vector_store_id") {
            if *v != store.0 {
                return Err(Failure::upstream());
            }
            *v = public(store.1).into();
        }
        let key = if search { "file_id" } else { "id" };
        let pid = label(&row[key])?;
        row[key] = file(pid).await?.into();
        Ok(())
    };
    if let Some(rows) = body["data"].as_array_mut() {
        for row in rows {
            rewrite(row).await?;
        }
        for key in ["first_id", "last_id"] {
            if let Some(pid) = body[key].as_str() {
                body[key] = file(pid).await?.into();
            }
        }
    } else {
        rewrite(body).await?;
    }
    Ok(())
}

async fn poll(node: &Node) -> Result<bool, Failure> {
    let Some(store) = node.db.claim_vector().await? else {
        return Ok(false);
    };
    let result = sample(node, &store).await;
    if result.is_err() && store.provider_id.is_some() {
        node.db.vector_missed(store.id, store.stamp).await?;
    }
    result
}

async fn sample(node: &Node, store: &crate::control::vectors::Store) -> Result<bool, Failure> {
    let index = node.current().ok_or_else(Failure::unavailable)?;
    let Some(pid) = store.provider_id.as_deref() else {
        node.db
            .observe_vector(Observation {
                handle: store.id,
                key: None,
                revision: index.revision,
                provider: None,
                stamp: store.stamp,
                metadata: None,
                state: "unknown",
                uncertain: true,
            })
            .await?;
        return Ok(true);
    };
    let dep = index
        .deployment(store.deployment_id)
        .filter(|dep| {
            dep.credential_id == store.credential_id
                && dep.credential_version == store.credential_version
                && dep.upstream_version == store.upstream_version
        })
        .ok_or_else(Failure::unavailable)?;
    // Billing retires a store whose prepaid storage lapsed under the delete policy.
    let method = if store.retire {
        Method::DELETE
    } else {
        Method::GET
    };
    let reply = call(node, &index, dep, method, &[pid.to_owned()])
        .await?
        .send()
        .await
        .map_err(|_| Failure::upstream())?;
    let (meta, state, uncertain) = if store.retire && reply.status().is_success() {
        (None, "deleted", false)
    } else if reply.status() == StatusCode::NOT_FOUND {
        (None, "deleted", true)
    } else {
        if !reply.status().is_success() {
            return Err(Failure::upstream());
        }
        let bytes = read_body(reply, node.upstream.response_limit())
            .await
            .map_err(|_| Failure::upstream())?;
        let v: Value = serde_json::from_slice(&bytes).map_err(|_| Failure::upstream())?;
        if v["id"] != pid {
            return Err(Failure::upstream());
        }
        let (meta, state) = view(store.id, &v)?;
        (Some(meta), state, false)
    };
    node.db
        .observe_vector(Observation {
            handle: store.id,
            key: None,
            revision: index.revision,
            provider: Some(pid),
            stamp: store.stamp,
            metadata: meta.as_ref(),
            state,
            uncertain,
        })
        .await?;
    Ok(true)
}
pub(super) async fn supervise(node: Arc<Node>, stop: CancellationToken) {
    loop {
        let result = tokio::select! {_=stop.cancelled()=>return,v=poll(&node)=>v};
        if result.is_err() {
            tracing::warn!(
                reason = "vector_store_poll_failed",
                "storage observation unavailable"
            );
        }
        if matches!(result, Ok(true)) {
            continue;
        }
        tokio::select! {_=stop.cancelled()=>return,_=tokio::time::sleep(Duration::from_secs(1))=>{}}
    }
}
