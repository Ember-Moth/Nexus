//! OpenAI container and artifact access. The authenticated resource path supplies the
//! provider account; only a verified allocation or a file inside that container can mint an ID.
use super::{BodyLimit, Node, error};
use crate::{
    control::{
        database::StoredResponse,
        snapshot::{Deployment, Index},
    },
    protocol::{
        Endpoint,
        execution::{ContainerTarget, MemoryTier, isolated},
        request::{Generate, Operation, Route},
    },
    request::{Failure, lifecycle::credential},
    upstream::{Upstream, read_body},
};
use axum::{
    Extension, Json,
    body::{Body, Bytes, to_bytes},
    extract::{Request, State},
    response::{IntoResponse, Response},
};
use http::{Method, StatusCode, header};
use serde_json::{Value, json};
use std::{sync::Arc, time::Duration};
use uuid::Uuid;

const EP: Endpoint = Endpoint::OpenaiResponses;
const ROOT: &str = "/v1/containers";
fn bad() -> Failure {
    Failure(StatusCode::BAD_REQUEST, "invalid_container_request")
}
fn absent() -> Failure {
    Failure(StatusCode::NOT_FOUND, "container_not_found")
}
fn resource(e: crate::control::ControlError) -> Failure {
    if e.database_message() == Some("resource_not_found") {
        absent()
    } else {
        e.into()
    }
}
fn parse(s: &str, prefix: &str) -> Result<Uuid, Failure> {
    s.strip_prefix(prefix)
        .and_then(|v| v.parse().ok())
        .ok_or_else(absent)
}
fn provider_id(v: &Value) -> Result<&str, Failure> {
    v.as_str()
        .filter(|v| {
            !v.is_empty()
                && v.len() <= 200
                && v.bytes()
                    .all(|b| b.is_ascii_alphanumeric() || b"-_".contains(&b))
        })
        .ok_or_else(Failure::upstream)
}
fn tier(v: &Value) -> Result<MemoryTier, Failure> {
    MemoryTier::parse(v.as_str().ok_or_else(bad)?).map_err(|_| bad())
}

pub(super) async fn bind(
    node: &Node,
    index: &Index,
    raw: &str,
    req: &mut Generate,
) -> Result<(), Failure> {
    let history = req.historical_container_fields();
    let existing = req.interpreter.as_ref().and_then(|t| t.target.existing);
    if existing.is_none() && history.is_empty() {
        return Ok(());
    }
    let kid = index.authenticate(raw)?.id;
    if let Some(c) = existing {
        let record = node
            .db
            .container(c, kid, index.revision, EP)
            .await
            .map_err(resource)?;
        if record.metadata["status"] == "expired" {
            return Err(Failure(StatusCode::CONFLICT, "container_expired"));
        }
        req.bind_deployment(record.model, record.deployment)
            .map_err(|_| bad())?;
        req.bind_interpreter(record.provider_id, tier(&record.metadata["memory_limit"])?);
    }
    for f in history {
        let (binding, fields) = history_field(node, index, kid, req.body(), &f).await?;
        req.bind_deployment(binding.0, binding.1)
            .map_err(|_| bad())?;
        for (path, pid) in fields {
            req.set_file(&path, pid);
        }
    }
    Ok(())
}

async fn history_field(
    node: &Node,
    index: &Index,
    kid: Uuid,
    item: &Value,
    f: &crate::protocol::execution::ContainerField,
) -> Result<((Uuid, Uuid), Vec<(String, String)>), Failure> {
    let cid = parse(
        item.pointer(&f.path)
            .and_then(Value::as_str)
            .ok_or_else(bad)?,
        "cntr_nx_",
    )?;
    let c = node
        .db
        .container(cid, kid, index.revision, EP)
        .await
        .map_err(resource)?;
    if c.metadata["status"] == "expired" {
        return Err(Failure(StatusCode::CONFLICT, "container_expired"));
    }
    let mut fields = vec![(f.path.clone(), c.provider_id)];
    if let Some(path) = &f.file {
        let fid = parse(
            item.pointer(path).and_then(Value::as_str).ok_or_else(bad)?,
            "cfile_nx_",
        )?;
        let pid = node
            .db
            .container_file(cid, kid, index.revision, fid, false)
            .await
            .map_err(resource)?;
        fields.push((path.clone(), pid));
    }
    Ok(((c.model, c.deployment), fields))
}
pub(super) async fn bind_restored(
    node: &Node,
    index: &Index,
    kid: Uuid,
    target: (Uuid, Uuid),
    item: &mut Value,
) -> Result<(), Failure> {
    for f in crate::protocol::execution::openai_fields(item) {
        let (binding, fields) = history_field(node, index, kid, item, &f).await?;
        if binding != target {
            return Err(bad());
        }
        for (path, pid) in fields {
            *item.pointer_mut(&path).ok_or_else(bad)? = pid.into();
        }
    }
    Ok(())
}

enum Payload {
    Empty,
    Data(Value),
    Upload(reqwest::multipart::Form),
}
async fn payload(
    bytes: Bytes,
    ct: Option<&http::HeaderValue>,
    file_post: bool,
) -> Result<Payload, Failure> {
    let ct = ct.and_then(|v| v.to_str().ok()).unwrap_or("");
    if ct.starts_with("multipart/form-data") {
        if !file_post {
            return Err(bad());
        }
        let boundary = multer::parse_boundary(ct).map_err(|_| bad())?;
        let stream = futures_util::stream::once(async move { Ok::<_, std::io::Error>(bytes) });
        let mut multipart = multer::Multipart::new(stream, boundary);
        let part = multipart
            .next_field()
            .await
            .map_err(|_| bad())?
            .ok_or_else(bad)?;
        let result = match part.name() {
            Some("file") => {
                let name = part.file_name().unwrap_or("file").to_owned();
                let mime = part
                    .content_type()
                    .map(ToString::to_string)
                    .unwrap_or_else(|| "application/octet-stream".into());
                let data = part.bytes().await.map_err(|_| bad())?;
                let file = reqwest::multipart::Part::bytes(data.to_vec())
                    .file_name(name)
                    .mime_str(&mime)
                    .map_err(|_| bad())?;
                Payload::Upload(reqwest::multipart::Form::new().part("file", file))
            }
            Some("file_id") => {
                Payload::Data(json!({"file_id":part.text().await.map_err(|_|bad())?}))
            }
            _ => return Err(bad()),
        };
        if multipart.next_field().await.map_err(|_| bad())?.is_some() {
            return Err(bad());
        }
        return Ok(result);
    }
    if bytes.is_empty() {
        return Ok(Payload::Empty);
    }
    let value: Value = serde_json::from_slice(&bytes).map_err(|_| bad())?;
    if !value.is_object() {
        return Err(bad());
    }
    Ok(Payload::Data(value))
}

async fn call(
    node: &Node,
    index: &Index,
    dep: &Deployment,
    method: Method,
    path: &[String],
) -> Result<reqwest::RequestBuilder, Failure> {
    let key = credential(node, index, dep).await?;
    let mut url = Upstream::endpoint(&dep.base_url, EP, Operation::Generate);
    url.path_segments_mut()
        .map_err(|_| Failure::unavailable())?
        .pop()
        .push("containers")
        .extend(path);
    Ok(node
        .upstream
        .client
        .request(method, url)
        .bearer_auth(key.as_str())
        .timeout(Duration::from_secs(90)))
}

pub(super) async fn handle(
    State(node): State<Arc<Node>>,
    Extension(trace): Extension<Uuid>,
    Extension(BodyLimit(limit)): Extension<BodyLimit>,
    req: Request,
) -> Response {
    let result = async {
        let (parts, body) = req.into_parts();
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
        let reader = Containers {
            node: node.clone(),
            index,
            kid,
            raw,
        };
        node.tasks
            .spawn(async move {
                let _permit = permit;
                reader.operation(trace, parts, bytes).await
            })
            .await
            .map_err(|_| Failure::unavailable())?
    }
    .await;
    result.unwrap_or_else(|e| error(trace, EP, e))
}
struct Containers {
    node: Arc<Node>,
    index: Arc<Index>,
    kid: Uuid,
    raw: String,
}
impl Containers {
    async fn open_file(&self, local: &str) -> Result<StoredResponse, Failure> {
        self.node
            .db
            .native_file(parse(local, "file_nx_")?, self.kid, self.index.revision, EP)
            .await
            .map_err(resource)
    }
    async fn metadata(
        &self,
        handle: Uuid,
        record: &StoredResponse,
        trace: Uuid,
    ) -> Result<Value, Failure> {
        let dep = self
            .index
            .deployment(record.deployment)
            .ok_or_else(Failure::unavailable)?;
        let upstream = call(
            &self.node,
            &self.index,
            dep,
            Method::GET,
            std::slice::from_ref(&record.provider_id),
        )
        .await?;
        let t = ContainerTarget {
            existing: Some(handle),
            preloaded: false,
            tier: tier(&record.metadata["memory_limit"])?,
        };
        self.node
            .db
            .admit_container(
                trace,
                self.kid,
                self.index.revision,
                (record.model, dep.id),
                t,
                false,
            )
            .await?;
        let reply = upstream.send().await.map_err(|_| Failure::upstream())?;
        if !reply.status().is_success() {
            return Err(Failure::upstream());
        }
        let wire = read_body(reply, self.node.upstream.response_limit())
            .await
            .map_err(|_| Failure::upstream())?;
        let mut v: Value = serde_json::from_slice(&wire).map_err(|_| Failure::upstream())?;
        if v["object"] != "container" || v["id"] != record.provider_id {
            return Err(Failure::upstream());
        }
        let local = self
            .node
            .db
            .container_receipt(trace, self.kid, &record.provider_id, Some(&v), false)
            .await?;
        v["id"] = format!("cntr_nx_{local}").into();
        Ok(v)
    }
    async fn operation(
        &self,
        trace: Uuid,
        parts: http::request::Parts,
        bytes: Bytes,
    ) -> Result<Response, Failure> {
        let path: Vec<_> = parts
            .uri
            .path()
            .strip_prefix(ROOT)
            .ok_or_else(absent)?
            .split('/')
            .filter(|s| !s.is_empty())
            .collect();
        let db = &self.node.db;
        let rev = self.index.revision;
        let create = path.is_empty() && parts.method == Method::POST;
        let file_post = path.len() == 2 && parts.method == Method::POST;
        let remove = parts.method == Method::DELETE;
        let mut body = payload(bytes, parts.headers.get(header::CONTENT_TYPE), file_post).await?;
        let mut query = Vec::new();
        let mut asc = false;
        let mut limit = 20i32;
        let mut cursor = None;
        for (k, v) in url::form_urlencoded::parse(parts.uri.query().unwrap_or("").as_bytes()) {
            match k.as_ref() {
                "after" if cursor.is_none() => cursor = Some(v.to_string()),
                "limit" => {
                    limit = v
                        .parse()
                        .ok()
                        .filter(|n| (1..=100).contains(n))
                        .ok_or_else(bad)?
                }
                "order" if v == "asc" || v == "desc" => asc = v == "asc",
                _ => return Err(bad()),
            }
            query.push((k.into_owned(), v.into_owned()));
        }
        if !query.is_empty()
            && (parts.method != Method::GET || !(path.is_empty() || path.len() == 2))
        {
            return Err(bad());
        }
        if path.is_empty() && !create {
            let mut rows = db
                .container_page(
                    self.kid,
                    rev,
                    cursor
                        .as_deref()
                        .map(|s| parse(s, "cntr_nx_"))
                        .transpose()?,
                    asc,
                    limit + 1,
                )
                .await
                .map_err(resource)?;
            let more = rows.len() > limit as usize;
            rows.truncate(limit as usize);
            let mut data = Vec::with_capacity(rows.len());
            for row in rows {
                let id = serde_json::from_value::<Uuid>(row["id"].clone())
                    .map_err(|_| Failure::upstream())?;
                let mut v = row["metadata"].clone();
                if v["name"].as_str().is_none()
                    || v["created_at"].as_i64().is_none()
                    || v["status"].as_str().is_none()
                {
                    let record = db
                        .container(id, self.kid, rev, EP)
                        .await
                        .map_err(resource)?;
                    v = self.metadata(id, &record, Uuid::new_v4()).await?;
                } else {
                    v["id"] = format!("cntr_nx_{id}").into();
                }
                data.push(v);
            }
            return Ok(Json(json!({"object":"list","first_id":data.first().map(|v|&v["id"]),"last_id":data.last().map(|v|&v["id"]),"has_more":more,"data":data})).into_response());
        }
        let mut remote = Vec::<String>::new();
        let mut bound = None;
        let mut memory = MemoryTier::One;
        if create {
            let Payload::Data(v) = &mut body else {
                return Err(bad());
            };
            let o = v.as_object().ok_or_else(bad)?;
            if o.keys().any(|k| {
                ![
                    "name",
                    "memory_limit",
                    "expires_after",
                    "file_ids",
                    "network_policy",
                ]
                .contains(&k.as_str())
            }) || v["name"].as_str().is_none_or(str::is_empty)
                || !isolated(v.get("network_policy"))
            {
                return Err(bad());
            }
            if !v["memory_limit"].is_null() {
                memory = tier(&v["memory_limit"])?;
            }
            if let Some(ids) = v.get_mut("file_ids").filter(|v| !v.is_null()) {
                for field in ids.as_array_mut().ok_or_else(bad)? {
                    let file = self.open_file(field.as_str().ok_or_else(bad)?).await?;
                    let target = (file.model, file.deployment);
                    if bound.is_some_and(|old| old != target) {
                        return Err(bad());
                    }
                    bound = Some(target);
                    *field = file.provider_id.into();
                }
            }
        } else if file_post {
            match &mut body {
                Payload::Data(v) if v.as_object().is_some_and(|o| o.len() == 1) => {
                    let file = self
                        .open_file(v["file_id"].as_str().ok_or_else(bad)?)
                        .await?;
                    bound = Some((file.model, file.deployment));
                    v["file_id"] = file.provider_id.into();
                }
                Payload::Upload(_) => {}
                _ => return Err(bad()),
            }
        } else if !matches!(body, Payload::Empty) {
            return Err(bad());
        }
        let (existing, model, dep) = if create {
            let names = match parts.headers.get("x-nexus-model") {
                Some(v) => vec![v.to_str().map_err(|_| bad())?],
                None => self.index.model_names(),
            };
            let mut target = None;
            for name in names {
                let mut req =
                    Generate::parse_compact(json!({"model":name,"input":[]})).map_err(|_| bad())?;
                if let Some((m, d)) = bound {
                    req.bind_deployment(m, d).map_err(|_| bad())?;
                }
                if let Ok(s) = self.index.container_target(&self.raw, &req, memory) {
                    target = Some((s.model, s.deployment));
                    break;
                }
            }
            let (m, d) =
                target.ok_or(Failure(StatusCode::BAD_REQUEST, "no_container_deployment"))?;
            (None, m, d)
        } else {
            let h = parse(path[0], "cntr_nx_")?;
            let r = if remove && path.len() == 1 {
                db.cleanup_container(trace, h, self.kid, rev).await
            } else {
                db.container(h, self.kid, rev, EP).await
            }
            .map_err(resource)?;
            if bound.is_some_and(|old| old != (r.model, r.deployment)) {
                return Err(bad());
            }
            memory = tier(&r.metadata["memory_limit"])?;
            if r.metadata["status"] == "expired" && !remove {
                if path.len() != 1 {
                    return Err(Failure(StatusCode::CONFLICT, "container_expired"));
                }
                let mut v = r.metadata;
                v["id"] = path[0].into();
                return Ok(Json(v).into_response());
            }
            remote.push(r.provider_id);
            (
                Some(h),
                r.model,
                self.index
                    .deployment(r.deployment)
                    .ok_or_else(Failure::unavailable)?,
            )
        };
        let mut fid = None;
        if path.len() >= 2 {
            remote.push("files".into());
        }
        if let Some(s) = path.get(2) {
            let file = parse(s, "cfile_nx_")?;
            fid = Some(file);
            remote.push(
                db.container_file(existing.ok_or_else(absent)?, self.kid, rev, file, false)
                    .await
                    .map_err(resource)?,
            );
        }
        if path.len() == 4 {
            remote.push("content".into());
        }
        for (k, v) in &mut query {
            if k == "after" {
                *v = db
                    .container_file(
                        existing.ok_or_else(absent)?,
                        self.kid,
                        rev,
                        parse(v, "cfile_nx_")?,
                        false,
                    )
                    .await
                    .map_err(resource)?;
            }
        }
        let mut request = call(&self.node, &self.index, dep, parts.method, &remote)
            .await?
            .query(&query);
        match body {
            Payload::Data(v) => request = request.json(&v),
            Payload::Upload(f) => request = request.multipart(f),
            Payload::Empty => {}
        }
        let cleanup = remove && path.len() == 1;
        if !cleanup {
            db.admit_container(
                trace,
                self.kid,
                rev,
                (model, dep.id),
                ContainerTarget {
                    existing,
                    tier: memory,
                    preloaded: bound.is_some(),
                },
                create,
            )
            .await?;
        }
        let reply = match request.send().await {
            Ok(r) => r,
            Err(e) => {
                if !cleanup && e.is_connect() {
                    db.unused_container(trace, self.kid, true).await?;
                }
                return Err(Failure::upstream());
            }
        };
        let status = reply.status();
        let mime = reply.headers().get(header::CONTENT_TYPE).cloned();
        let wire = read_body(reply, self.node.upstream.response_limit())
            .await
            .map_err(|_| Failure::upstream())?;
        if !status.is_success() {
            if cleanup && status == StatusCode::NOT_FOUND {
                db.retired_container(trace, self.kid, &remote[0], false)
                    .await?;
            } else if !cleanup && matches!(status.as_u16(), 400 | 401 | 403 | 404 | 413 | 422 | 429)
            {
                db.unused_container(trace, self.kid, true).await?;
            }
            let value: Value = serde_json::from_slice(&wire).map_err(|_| Failure::upstream())?;
            return Ok((status, Json(value)).into_response());
        }
        if path.len() == 4 {
            db.container_receipt(trace, self.kid, &remote[0], None, false)
                .await?;
            let mut res = Response::new(Body::from(wire));
            if let Some(m) = mime {
                res.headers_mut().insert(header::CONTENT_TYPE, m);
            }
            return Ok(res);
        }
        let mut v: Value = serde_json::from_slice(&wire).map_err(|_| Failure::upstream())?;
        if cleanup {
            if v["object"] != "container.deleted" || v["deleted"] != true || v["id"] != remote[0] {
                return Err(Failure::upstream());
            }
            db.retired_container(trace, self.kid, &remote[0], true)
                .await?;
            v["id"] = path[0].into();
        } else if path.len() <= 1 {
            let pid = provider_id(&v["id"])?;
            if v["object"] != "container"
                || remote.first().is_some_and(|s| s != pid)
                || !isolated(v.get("network_policy"))
            {
                return Err(Failure::upstream());
            }
            let local = db
                .container_receipt(trace, self.kid, pid, Some(&v), false)
                .await?;
            v["id"] = format!("cntr_nx_{local}").into();
        } else {
            let c = db
                .container_receipt(trace, self.kid, &remote[0], None, false)
                .await?;
            if Some(c) != existing {
                return Err(Failure::upstream());
            }
            if file_post && v["object"] != "container.file" {
                return Err(Failure::upstream());
            }
            if let Some(rows) = v["data"].as_array_mut() {
                for row in rows {
                    self.file_view(c, &remote[0], row, None).await?;
                }
                for k in ["first_id", "last_id"] {
                    if let Some(pid) = v[k].as_str() {
                        let local = db.container_file_id(c, self.kid, rev, pid, false).await?;
                        v[k] = format!("cfile_nx_{local}").into();
                    }
                }
            } else if remove {
                if v["object"] != "container.file.deleted"
                    || v["id"] != remote[2]
                    || v["deleted"] != true
                    || v.get("container_id")
                        .is_some_and(|id| id != remote[0].as_str())
                {
                    return Err(Failure::upstream());
                }
                let id = fid.ok_or_else(bad)?;
                db.container_file(c, self.kid, rev, id, true).await?;
                v["id"] = format!("cfile_nx_{id}").into();
                if v.get("container_id").is_some() {
                    v["container_id"] = path[0].into();
                }
            } else {
                self.file_view(c, &remote[0], &mut v, remote.get(2).map(String::as_str))
                    .await?;
            }
        }
        Ok((status, Json(v)).into_response())
    }
    async fn file_view(
        &self,
        c: Uuid,
        pid: &str,
        v: &mut Value,
        expected: Option<&str>,
    ) -> Result<(), Failure> {
        if v["object"] != "container.file" || v["container_id"] != pid {
            return Err(Failure::upstream());
        }
        let f = provider_id(&v["id"])?;
        if expected.is_some_and(|s| s != f) {
            return Err(Failure::upstream());
        }
        let id = self
            .node
            .db
            .container_file_id(c, self.kid, self.index.revision, f, true)
            .await?;
        v["id"] = format!("cfile_nx_{id}").into();
        v["container_id"] = format!("cntr_nx_{c}").into();
        Ok(())
    }
}
