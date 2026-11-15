//! Native Anthropic batch submission, tenant resource operations and reconciliation.
use super::{BodyLimit, Node, bind_input, error};
use crate::{
    control::{
        batches::{Batch, Item},
        limits::{Admission, LimitError},
        snapshot::{Deployment, Index},
    },
    protocol::{
        Endpoint,
        request::{AnthropicHeaders, Generate, Operation, Route},
        response,
    },
    request::{Failure, lifecycle::credential},
    upstream::{Upstream, read_body},
};
use axum::{
    Extension, Json,
    body::{Body, to_bytes},
    extract::{Request, State},
    response::{IntoResponse, Response},
};
use futures_util::StreamExt;
use http::{HeaderMap, Method, StatusCode, header};
use serde_json::{Value, json};
use std::{
    collections::{HashMap, HashSet},
    sync::Arc,
    time::Duration,
};
use tokio_util::sync::CancellationToken;
use uuid::Uuid;

const EP: Endpoint = Endpoint::AnthropicMessages;
const ROOT: &str = "/v1/messages/batches";
fn invalid() -> Failure {
    Failure(StatusCode::BAD_REQUEST, "invalid_message_batch")
}
fn missing() -> Failure {
    Failure(StatusCode::NOT_FOUND, "batch_not_found")
}
fn id(s: &str) -> Result<Uuid, Failure> {
    s.strip_prefix("msgbatch_nx_")
        .and_then(|s| s.parse().ok())
        .ok_or_else(missing)
}
fn public(id: Uuid) -> String {
    format!("msgbatch_nx_{id}")
}
fn identifier(s: &str, max: usize) -> bool {
    !s.is_empty()
        && s.len() <= max
        && s.bytes()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, b'-' | b'_'))
}

fn metadata(handle: Uuid, data: &Value) -> Result<Value, Failure> {
    if data["type"] != "message_batch"
        || !matches!(
            data["processing_status"].as_str(),
            Some("in_progress" | "canceling" | "ended")
        )
        || !data["id"].as_str().is_some_and(|s| identifier(s, 200))
    {
        return Err(Failure::upstream());
    }
    let mut counts = json!({});
    for k in ["processing", "succeeded", "errored", "canceled", "expired"] {
        counts[k] = data["request_counts"][k]
            .as_u64()
            .filter(|&n| n <= 100000)
            .ok_or_else(Failure::upstream)?
            .into();
    }
    let mut view = json!({"id":public(handle),"type":"message_batch","processing_status":data["processing_status"],"request_counts":counts,
        "results_url":data["results_url"].as_str().map(|_|format!("{ROOT}/{}/results",public(handle)))});
    for k in [
        "created_at",
        "expires_at",
        "ended_at",
        "cancel_initiated_at",
        "archived_at",
    ] {
        if let Some(v) = data.get(k) {
            if !v.is_null()
                && !v
                    .as_str()
                    .is_some_and(|s| chrono::DateTime::parse_from_rfc3339(s).is_ok())
            {
                return Err(Failure::upstream());
            }
            view[k] = v.clone();
        }
    }
    Ok(view)
}

async fn upstream(
    node: &Node,
    index: &Index,
    dep: &Deployment,
    method: Method,
    segments: &[&str],
    headers: &AnthropicHeaders,
) -> Result<reqwest::RequestBuilder, Failure> {
    let secret = credential(node, index, dep).await?;
    let mut url = Upstream::endpoint(&dep.base_url, EP, Operation::Generate);
    url.path_segments_mut()
        .map_err(|_| Failure::unavailable())?
        .push("batches")
        .extend(segments.iter().copied());
    Ok(headers
        .apply(
            node.upstream
                .client
                .request(method, url)
                .header("x-api-key", secret.as_str()),
        )
        .timeout(Duration::from_secs(100)))
}

async fn data(reply: reqwest::Response, limit: usize) -> Result<Value, Failure> {
    let bytes = read_body(reply, limit)
        .await
        .map_err(|_| Failure::upstream())?;
    serde_json::from_slice(&bytes).map_err(|_| Failure::upstream())
}

pub(super) async fn handle(
    State(node): State<Arc<Node>>,
    Extension(trace): Extension<Uuid>,
    Extension(BodyLimit(limit)): Extension<BodyLimit>,
    req: Request,
) -> Response {
    handle_request(node, trace, limit, req)
        .await
        .unwrap_or_else(|e| error(trace, EP, e))
}

async fn handle_request(
    node: Arc<Node>,
    trace: Uuid,
    limit: usize,
    req: Request,
) -> Result<Response, Failure> {
    let (parts, body) = req.into_parts();
    let raw = Route::CountTokens
        .key(&parts.headers)
        .map_err(|_| Failure(StatusCode::UNAUTHORIZED, "invalid_api_key"))?;
    let headers = AnthropicHeaders::parse(&parts.headers).map_err(|_| invalid())?;
    let index = node.current().ok_or_else(Failure::unavailable)?;
    let kid = index.authenticate(raw)?.id;
    let permit = node
        .capacity
        .clone()
        .try_acquire_owned()
        .map_err(|_| Failure::unavailable())?;
    let tail = parts.uri.path().strip_prefix(ROOT).ok_or_else(missing)?;
    if tail.is_empty() {
        if parts.method == Method::GET {
            let mut cursor = None;
            let mut newer = false;
            let mut limit = 20;
            for (k, v) in url::form_urlencoded::parse(parts.uri.query().unwrap_or("").as_bytes()) {
                match k.as_ref() {
                    "before_id" | "after_id" if cursor.is_none() => {
                        cursor = Some(id(&v)?);
                        newer = k == "before_id";
                    }
                    "limit" => {
                        limit = v
                            .parse::<i32>()
                            .ok()
                            .filter(|n| (1..=1000).contains(n))
                            .ok_or_else(invalid)?
                    }
                    _ => return Err(invalid()),
                }
            }
            let mut rows = node
                .db
                .batch_page(kid, index.revision, cursor, newer, limit + 1)
                .await?;
            let more = rows.len() > limit as usize;
            rows.truncate(limit as usize);
            return Ok(Json(json!({"has_more":more,"first_id":rows.first().map(|v|&v["id"]),"last_id":rows.last().map(|v|&v["id"]),"data":rows})).into_response());
        }
        let bytes = to_bytes(body, limit)
            .await
            .map_err(|_| Failure(StatusCode::PAYLOAD_TOO_LARGE, "batch_too_large"))?;
        let mut input: Value = serde_json::from_slice(&bytes).map_err(|_| invalid())?;
        let object = input
            .as_object_mut()
            .filter(|o| o.len() == 1)
            .ok_or_else(invalid)?;
        let list = object.remove("requests").ok_or_else(invalid)?;
        let items = list
            .as_array()
            .filter(|a| !a.is_empty() && a.len() <= 100000)
            .ok_or_else(invalid)?;
        let mut seen = HashSet::new();
        let mut records = Vec::with_capacity(items.len());
        let mut prepared = Vec::with_capacity(items.len());
        let mut account = None;
        let mut upper = 0;
        for item in items {
            let custom = item["custom_id"]
                .as_str()
                .filter(|s| identifier(s, 64) && seen.insert(*s))
                .ok_or_else(invalid)?;
            if item.as_object().is_none_or(|o| o.len() != 2)
                || item["params"].get("speed").is_some()
                || item["params"].get("fallbacks").is_some()
            {
                return Err(invalid());
            }
            let mut request =
                Generate::parse(EP, item["params"].clone(), false).map_err(|_| invalid())?;
            if request.stream {
                return Err(invalid());
            }
            request.anthropic = headers.clone();
            bind_input(&node, &index, raw, &mut request).await?;
            let s = index.native_messages(raw, &request, account)?;
            account.get_or_insert(s.deployment);
            upper += u64::from(s.bounds.inputs) + u64::from(s.bounds.outputs);
            prepared.push(json!({"custom_id":custom,"params":s.body}));
            records.push(Item {
                custom_id: custom.to_owned(),
                model: s.model,
                deployment: s.deployment.id,
                rid: Uuid::new_v4(),
                aid: Uuid::new_v4(),
                bounds: s.bounds,
            });
        }
        let dep = account.ok_or_else(invalid)?;
        let call = upstream(&node, &index, dep, Method::POST, &[], &headers)
            .await?
            .json(&json!({"requests":prepared}));
        let mut lease = node
            .limits
            .acquire_weighted(
                Admission {
                    key: index.authenticate(raw)?,
                    request: trace,
                    node: index.node,
                    model: records[0].model,
                    revision: index.revision,
                    bounds: records[0].bounds,
                    quotas: &index.quotas,
                    ttl_ms: node.lease_ms,
                },
                records.len(),
                upper,
            )
            .await
            .map_err(|e| match e {
                LimitError::Limited => Failure(StatusCode::TOO_MANY_REQUESTS, "rate_limited"),
                _ => Failure::unavailable(),
            })?;
        let dep_id = dep.id;
        let tracker = node.tasks.clone();
        // Continue the single submission and record its provider binding if the caller disconnects.
        let task = tracker.spawn(async move {
            let _permit = permit;
            let mut sent = false;
            let submit = async {
                node.db.batch_prepare(trace, kid, index.revision, dep_id, &records, headers.negotiated()).await?;
                sent = true;
                let reply = call.send().await.map_err(|_| Failure::upstream())?;
                let status = reply.status();
                let object = data(reply, node.upstream.response_limit()).await?;
                if !status.is_success() {
                    let rejected = matches!(status.as_u16(), 400|401|403|404|413|422|429) && object["type"] == "error";
                    let outcome = if rejected { "failed" } else { "unknown" };
                    node.db.batch_finish(trace, None, None, json!({}), outcome).await?;
                    return Ok(super::reply(status, object, Endpoint::AnthropicMessages));
                }
                let view = metadata(trace, &object)?;
                let pid = object["id"].as_str().ok_or_else(Failure::upstream)?;
                let record = || node.db.batch_record(trace, kid, index.revision, pid, &view, false);
                if let Err(e) = record().await {
                    if !e.retryable() { return Err(e.into()); }
                    record().await?;
                }
                Ok((status, Json(view)).into_response())
            };
            let result = {
                tokio::pin!(submit);
                loop {
                    tokio::select! {
                        biased;
                        _ = tokio::time::sleep_until(lease.renewal_at()) => {
                            if node.limits.renew(&mut lease).await.is_err() { break Err(Failure::unavailable()); }
                        },
                        result = &mut submit => break result,
                    }
                }
            };
            let _ = node.limits.finish(&lease, if sent {None} else {Some(0)}).await;
            result
        });
        return task.await.map_err(|_| Failure::unavailable())?;
    }
    let (handle, op) = tail
        .trim_start_matches('/')
        .split_once('/')
        .unwrap_or((tail.trim_start_matches('/'), ""));
    let handle = id(handle)?;
    let b = node
        .db
        .batch_resolve(handle, kid, index.revision)
        .await
        .map_err(|e| {
            if e.database_message() == Some("resource_not_found") {
                missing()
            } else {
                e.into()
            }
        })?;
    let pid = b.provider_id.as_deref().ok_or_else(missing)?;
    if parts.method == Method::DELETE && !b.done {
        return Err(Failure(StatusCode::CONFLICT, "batch_pending"));
    }
    let dep = index
        .deployment(b.deployment_id)
        .ok_or_else(Failure::unavailable)?;
    let mut segments = vec![pid];
    if !op.is_empty() {
        segments.push(op);
    }
    let reply = upstream(
        &node,
        &index,
        dep,
        parts.method.clone(),
        &segments,
        &headers,
    )
    .await?
    .send()
    .await
    .map_err(|_| Failure::upstream())?;
    let status = reply.status();
    if status.is_success() && op == "results" {
        return Ok(results_response(
            node.clone(),
            handle,
            kid,
            index.revision,
            reply,
            permit,
        ));
    }
    let obj = data(reply, node.upstream.response_limit()).await?;
    if !status.is_success() {
        if status == StatusCode::NOT_FOUND && parts.method == Method::DELETE {
            node.db
                .batch_record(handle, kid, index.revision, pid, &Value::Null, true)
                .await?;
        }
        return Ok(super::reply(status, obj, Endpoint::AnthropicMessages));
    }
    if obj["id"] != pid {
        return Err(Failure::upstream());
    }
    let remove = parts.method == Method::DELETE;
    let view = if remove {
        if obj["type"] != "message_batch_deleted" {
            return Err(Failure::upstream());
        }
        json!({"id":public(handle),"type":"message_batch_deleted"})
    } else {
        metadata(handle, &obj)?
    };
    node.db
        .batch_record(handle, kid, index.revision, pid, &view, remove)
        .await?;
    Ok((status, Json(view)).into_response())
}

fn results_response(
    node: Arc<Node>,
    handle: Uuid,
    kid: Uuid,
    rev: i64,
    mut reply: reqwest::Response,
    permit: tokio::sync::OwnedSemaphorePermit,
) -> Response {
    let (tx, rx) = tokio::sync::mpsc::channel(2);
    let tasks = node.tasks.clone();
    tasks.spawn(async move{
        let _permit=permit;
        let result=async {
            let mut line=Vec::new();
            loop{
                let chunk=tokio::select!{_=tx.closed()=>return Ok::<_,Failure>(()),r=reply.chunk()=>r.map_err(|_|Failure::upstream())?};
                let Some(bytes)=chunk else{
                    if !line.is_empty(){result_line(&node,handle,kid,rev,&line,&tx).await?;}return Ok(());
                };
                for slice in bytes.split_inclusive(|&b|b==b'\n'){
                    if slice.len()>node.upstream.response_limit().saturating_sub(line.len()){return Err(Failure::upstream());}
                    line.extend_from_slice(slice);
                    if line.last()==Some(&b'\n'){
                        if !line.iter().all(u8::is_ascii_whitespace){result_line(&node,handle,kid,rev,&line,&tx).await?;}
                        line.clear();
                    }
                }
            }
        }.await;
        if result.is_err(){let _=tx.send(Err(std::io::Error::other("unavailable batch result"))).await;}
    });
    let stream =
        futures_util::stream::unfold(rx, |mut rx| async move { rx.recv().await.map(|v| (v, rx)) });
    Response::builder()
        .header(header::CONTENT_TYPE, "application/x-jsonl")
        .body(Body::from_stream(stream))
        .expect("JSONL response")
}

async fn result_line(
    node: &Node,
    handle: Uuid,
    kid: Uuid,
    rev: i64,
    line: &[u8],
    tx: &tokio::sync::mpsc::Sender<Result<axum::body::Bytes, std::io::Error>>,
) -> Result<(), Failure> {
    let mut row: Value = serde_json::from_slice(line).map_err(|_| Failure::upstream())?;
    let custom = row["custom_id"]
        .as_str()
        .filter(|s| identifier(s, 64))
        .ok_or_else(Failure::upstream)?
        .to_owned();
    if let Some(msg) = row.get_mut("result").and_then(|r| r.get_mut("message")) {
        let (containers, files) = crate::protocol::execution::output_fields(msg);
        if !containers.is_empty() || !files.is_empty() {
            let context = node.db.batch_execution(handle, &custom, kid, rev).await?;
            let model = serde_json::from_value(context["model"].clone())
                .map_err(|_| Failure::upstream())?;
            let dep = serde_json::from_value(context["deployment"].clone())
                .map_err(|_| Failure::upstream())?;
            crate::control::resources::ResourceScope::new(&node.db, kid, rev, model, dep)
                .execution(
                    EP,
                    if context["execution"] == true {
                        Some(
                            serde_json::from_value(context["rid"].clone())
                                .map_err(|_| Failure::upstream())?,
                        )
                    } else {
                        None
                    },
                    None,
                )
                .rewrite(msg)
                .await
                .map_err(|_| Failure::upstream())?;
        }
    }
    let mut wire = serde_json::to_vec(&row).map_err(|_| Failure::upstream())?;
    if wire.len() > node.upstream.response_limit() {
        return Err(Failure::upstream());
    }
    wire.push(b'\n');
    tokio::time::timeout(node.stream_idle, tx.send(Ok(wire.into())))
        .await
        .map_err(|_| Failure::unavailable())?
        .map_err(|_| Failure::unavailable())?;
    Ok(())
}

fn result_fact(
    line: &[u8],
    facts: &mut serde_json::Map<String, Value>,
    remaining: &mut HashMap<String, u64>,
) -> Result<(), Failure> {
    let row: Value = serde_json::from_slice(line).map_err(|_| Failure::upstream())?;
    let custom = row["custom_id"]
        .as_str()
        .filter(|s| identifier(s, 64))
        .ok_or_else(Failure::upstream)?;
    if facts.contains_key(custom) || facts.len() >= 100000 {
        return Err(Failure::upstream());
    }
    let result = &row["result"];
    let kind = result["type"].as_str().ok_or_else(Failure::upstream)?;
    let count = remaining.get_mut(kind).ok_or_else(Failure::upstream)?;
    *count = count.checked_sub(1).ok_or_else(Failure::upstream)?;
    let fact = match kind {
        "succeeded" => match response::observe(EP, &result["message"]) {
            Ok(done) => json!({"outcome":done.outcome(EP),"usage":done.usage.raw_usage}),
            Err(_) => json!({"outcome":"unknown"}),
        },
        // Anthropic bills only succeeded items. A terminal non-execution result is explicit evidence.
        "expired" | "errored" | "canceled" => {
            json!({"outcome":"failed","usage":{"input_tokens":0,"output_tokens":0,
            "cache_read_input_tokens":0,"cache_creation_input_tokens":0,"web_search_requests":0,"code_execution_requests":0}})
        }
        _ => return Err(Failure::upstream()),
    };
    facts.insert(custom.into(), fact);
    Ok(())
}

async fn result_facts(
    reply: reqwest::Response,
    limit: usize,
    counts: &Value,
) -> Result<Value, Failure> {
    let mut remaining: HashMap<String, u64> =
        serde_json::from_value(counts.clone()).map_err(|_| Failure::upstream())?;
    let mut facts = serde_json::Map::new();
    let mut line = Vec::new();
    let mut stream = reply.bytes_stream();
    while let Some(chunk) = stream.next().await {
        let chunk = chunk.map_err(|_| Failure::upstream())?;
        for slice in chunk.split_inclusive(|&b| b == b'\n') {
            if slice.len() > limit.saturating_sub(line.len()) {
                return Err(Failure::upstream());
            }
            line.extend_from_slice(slice);
            if line.last() == Some(&b'\n') {
                if !line.iter().all(u8::is_ascii_whitespace) {
                    result_fact(&line, &mut facts, &mut remaining)?;
                }
                line.clear();
            }
        }
    }
    if !line.is_empty() {
        result_fact(&line, &mut facts, &mut remaining)?;
    }
    if remaining.values().any(|n| *n != 0) {
        return Err(Failure::upstream());
    }
    Ok(Value::Object(facts))
}

async fn reconcile(node: &Node, job: Batch) -> Result<(), Failure> {
    let handle = job.id;
    let pid = job.provider_id.as_deref();
    let Some(pid) = pid else {
        node.db
            .batch_finish(handle, None, None, json!({}), "unknown")
            .await?;
        return Ok(());
    };
    let index = node.current().ok_or_else(Failure::unavailable)?;
    let dep = index
        .deployment(job.deployment_id)
        .filter(|d| {
            d.credential_id == job.credential_id
                && d.credential_version == job.credential_version
                && d.upstream_version == job.upstream_version
        })
        .ok_or_else(Failure::unavailable)?;
    let mut h = HeaderMap::new();
    for (k, v) in &job.headers {
        h.insert(
            header::HeaderName::try_from(k).map_err(|_| invalid())?,
            header::HeaderValue::try_from(v).map_err(|_| invalid())?,
        );
    }
    let headers = AnthropicHeaders::parse(&h).map_err(|_| invalid())?;
    let reply = upstream(node, &index, dep, Method::GET, &[pid], &headers)
        .await?
        .send()
        .await
        .map_err(|_| Failure::upstream())?;
    if reply.status() == StatusCode::NOT_FOUND {
        node.db
            .batch_finish(handle, Some(pid), None, json!({}), "unknown")
            .await?;
        return Ok(());
    }
    if !reply.status().is_success() {
        return Err(Failure::upstream());
    }
    let obj = data(reply, node.upstream.response_limit()).await?;
    if obj["id"] != pid {
        return Err(Failure::upstream());
    }
    let view = metadata(handle, &obj)?;
    if obj["processing_status"] != "ended" {
        node.db
            .batch_record(handle, job.api_key_id, index.revision, pid, &view, false)
            .await?;
        return Ok(());
    }
    let reply = upstream(node, &index, dep, Method::GET, &[pid, "results"], &headers)
        .await?
        .send()
        .await
        .map_err(|_| Failure::upstream())?;
    if !reply.status().is_success() {
        return Err(Failure::upstream());
    }
    let facts = result_facts(
        reply,
        node.upstream.response_limit(),
        &view["request_counts"],
    )
    .await?;
    node.db
        .batch_finish(handle, Some(pid), Some(&view), facts, "unknown")
        .await?;
    Ok(())
}

pub(super) async fn supervise(node: Arc<Node>, stop: CancellationToken) {
    loop {
        let poll = async {
            if let Some(job) = node.db.batch_claim().await? {
                let (id, pid, expired) = (
                    job.id,
                    job.provider_id.clone(),
                    chrono::Utc::now() - job.created_at > chrono::Duration::hours(25),
                );
                if let Err(e) = reconcile(&node, job).await {
                    if !expired {
                        return Err(e);
                    }
                    node.db
                        .batch_finish(id, pid.as_deref(), None, json!({}), "unknown")
                        .await?;
                }
                Ok(true)
            } else {
                Ok(false)
            }
        };
        let result = tokio::select! {_=stop.cancelled()=>return,r=poll=>r};
        match result {
            Ok(true) => continue,
            Err(_) => tracing::warn!(
                reason = "batch_poll_failed",
                "message batch reconciliation unavailable"
            ),
            Ok(false) => {}
        }
        tokio::select! {_=stop.cancelled()=>return,_=tokio::time::sleep(Duration::from_secs(1))=>{}}
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn each_jsonl_outcome_must_match_the_batch_counts() {
        let mut remaining = HashMap::from([("canceled".to_owned(), 1), ("expired".to_owned(), 1)]);
        let mut facts = serde_json::Map::new();
        let line = |id: &str, t: &str| {
            json!({"custom_id":id,"result":{"type":t}})
                .to_string()
                .into_bytes()
        };
        assert!(result_fact(&line("a", "canceled"), &mut facts, &mut remaining).is_ok());
        assert!(result_fact(&line("a", "expired"), &mut facts, &mut remaining).is_err());
        assert!(result_fact(&line("b", "canceled"), &mut facts, &mut remaining).is_err());
        assert!(result_fact(&line("b", "expired"), &mut facts, &mut remaining).is_ok());
        assert!(remaining.values().all(|n| *n == 0));
    }
}
