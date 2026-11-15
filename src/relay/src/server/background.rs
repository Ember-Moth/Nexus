//! Poll durable jobs as the admitting gateway principal. Terminal usage and job
//! acknowledgement commit together, including when the previous reply was lost.
use super::Node;
use crate::{
    protocol::{Endpoint, response, stream::Terminal},
    request::lifecycle::credential,
    upstream::{Upstream, read_body},
};
use serde_json::Value;
use std::{sync::Arc, time::Duration};
use tokio_util::sync::CancellationToken;
use uuid::Uuid;

async fn poll(node: &Node) -> Result<bool, ()> {
    let Some(job) = node.db.background_claim().await.map_err(|_| ())? else {
        return Ok(false);
    };
    let index = node.current().ok_or(())?;
    let dep = index.deployment(job.deployment).ok_or(())?;
    if dep.credential_id != job.credential
        || dep.credential_version != job.credential_version
        || dep.upstream_version != job.upstream_version
    {
        return Err(());
    }
    let secret = credential(node, &index, dep).await.map_err(|_| ())?;
    let mut url = Upstream::endpoint(
        &dep.base_url,
        Endpoint::OpenaiResponses,
        crate::protocol::request::Operation::Generate,
    );
    url.path_segments_mut().map_err(|_| ())?.push(&job.provider);
    let reply = node
        .upstream
        .client
        .get(url)
        .bearer_auth(secret.as_str())
        .timeout(Duration::from_secs(20))
        .send()
        .await
        .map_err(|_| ())?;
    if reply.status() == http::StatusCode::NOT_FOUND {
        node.db
            .background_finish(job.handle, "unknown", "unknown", Value::Null)
            .await
            .map_err(|_| ())?;
        return Ok(true);
    }
    if !reply.status().is_success() {
        return Err(());
    }
    let bytes = read_body(reply, node.upstream.response_limit())
        .await
        .map_err(|_| ())?;
    let mut data: Value = serde_json::from_slice(&bytes).map_err(|_| ())?;
    if data["id"] != job.provider || data["object"] != "response" {
        return Err(());
    }
    if matches!(data["status"].as_str(), Some("queued" | "in_progress")) {
        return Ok(true);
    }
    let ep = Endpoint::OpenaiResponses;
    let status = node.db.status(job.rid).await.map_err(|_| ())?.ok_or(())?;
    let metered = crate::protocol::hosted::SearchMeters {
        web: status["web_search_limit"].as_u64().is_some_and(|n| n > 0),
        files: status["file_search_limit"].as_u64().is_some_and(|n| n > 0),
    };
    let observe = |body: &Value| {
        response::observe_operation(
            ep,
            crate::protocol::request::Operation::Generate,
            body,
            metered,
        )
    };
    let terminal = observe(&data)
        .or_else(|err| {
            if data.get("usage").is_none() {
                return Err(err);
            }
            data.as_object_mut().unwrap().remove("usage");
            observe(&data)
        })
        .map_err(|_| ())?;
    let kid = serde_json::from_value(status["api_key_id"].clone()).map_err(|_| ())?;
    let fields = crate::protocol::execution::openai_fields(&data);
    let mut called = false;
    for field in fields.iter().filter(|f| f.call) {
        let pid = data
            .pointer(&field.path)
            .and_then(Value::as_str)
            .ok_or(())?;
        node.db
            .container_receipt(job.rid, kid, pid, None, field.completed)
            .await
            .map_err(|_| ())?;
        called = true;
    }
    if !called
        && data["status"] == "completed"
        && data["output"]
            .as_array()
            .is_some_and(|items| items.iter().all(|i| i["type"] != "code_interpreter_call"))
    {
        node.db
            .unused_container(job.rid, kid, false)
            .await
            .map_err(|_| ())?;
    }
    finish(&node.db, job.handle, &terminal).await?;
    Ok(true)
}

pub(crate) async fn finish(
    db: &crate::control::database::Gateway,
    handle: Uuid,
    terminal: &crate::protocol::stream::Completion,
) -> Result<(), ()> {
    let outcome = terminal.outcome(Endpoint::OpenaiResponses);
    let delivery = match (terminal.terminal, outcome) {
        (Terminal::Cancelled, _) => "client_cancelled",
        (_, "succeeded") => "succeeded",
        (_, "failed") => "upstream_failed",
        _ => "unknown",
    };
    db.background_finish(
        handle,
        outcome,
        delivery,
        serde_json::to_value(&terminal.usage.raw_usage).map_err(|_| ())?,
    )
    .await
    .map_err(|_| ())?;
    Ok(())
}

pub(super) async fn supervise(node: Arc<Node>, stop: CancellationToken) {
    loop {
        let result = tokio::select! {_=stop.cancelled()=>return,r=poll(&node)=>r};
        if result.is_err() {
            tracing::warn!(
                reason = "background_poll_failed",
                "background response reconciliation unavailable"
            );
        }
        if result == Ok(true) {
            continue;
        }
        tokio::select! {_=stop.cancelled()=>return,_=tokio::time::sleep(Duration::from_secs(1))=>{}}
    }
}

#[derive(serde::Deserialize)]
pub(crate) struct Job {
    pub(crate) rid: Uuid,
    pub(crate) handle: Uuid,
    pub(crate) deployment: Uuid,
    pub(crate) credential: Uuid,
    pub(crate) credential_version: i32,
    pub(crate) upstream_version: i32,
    pub(crate) provider: String,
}

/// A replay is a read of an already bound resource. Its stream never reports billable events.
pub(super) struct ReadBinding {
    pub interpreter: Option<Uuid>,
    pub id: String,
    pub record: crate::control::database::StoredResponse,
    pub key: Uuid,
    pub revision: i64,
}

impl ReadBinding {
    pub(super) async fn rewrite(&self, node: &Node, response: &mut Value) -> Result<(), ()> {
        if response["id"] != self.record.provider_id {
            return Err(());
        }
        response["id"] = self.id.clone().into();
        if let Some(conv) = response.get_mut("conversation").filter(|v| !v.is_null()) {
            let provider = conv["id"].as_str().ok_or(())?;
            let local = node
                .db
                .conversation_id(
                    self.key,
                    self.revision,
                    self.record.model,
                    self.record.deployment,
                    provider,
                )
                .await
                .map_err(|_| ())?
                .ok_or(())?;
            conv["id"] = format!("conv_nx_{local}").into();
        }
        Ok(())
    }
}

async fn replay(
    node: &Node,
    binding: &ReadBinding,
    mut upstream: reqwest::Response,
    mut previous: Option<i64>,
    tx: &tokio::sync::mpsc::Sender<Result<axum::body::Bytes, std::io::Error>>,
) -> Result<(), ()> {
    let mut decoder = crate::protocol::sse::Decoder::new(node.event_bytes);
    let mut resources = crate::control::resources::ResourceScope::new(
        &node.db,
        binding.key,
        binding.revision,
        binding.record.model,
        binding.record.deployment,
    )
    .interpreter(binding.interpreter);
    loop {
        let chunk = tokio::select! {
            _=tx.closed()=>return Ok(()),
            r=tokio::time::timeout(node.stream_idle,upstream.chunk())=>r.map_err(|_|())?.map_err(|_|())?,
        };
        let Some(bytes) = chunk else {
            return decoder.finish().map_err(|_| ());
        };
        for part in bytes.chunks(8192) {
            let mut events = Vec::new();
            decoder
                .feed(part, |e| {
                    events.push(e);
                    Ok(())
                })
                .map_err(|_| ())?;
            for e in events {
                let mut value: Value = serde_json::from_str(&e.data).map_err(|_| ())?;
                let kind = value["type"]
                    .as_str()
                    .filter(|s| {
                        !s.is_empty()
                            && s.bytes()
                                .all(|c| c.is_ascii_alphanumeric() || b"._-".contains(&c))
                    })
                    .ok_or(())?
                    .to_owned();
                if e.name.as_ref().is_some_and(|name| name != &kind) {
                    return Err(());
                }
                if kind != "keepalive" {
                    let n = value["sequence_number"]
                        .as_i64()
                        .filter(|&n| n >= 0)
                        .ok_or(())?;
                    if previous.is_some_and(|p| p >= n) {
                        return Err(());
                    }
                    previous = Some(n);
                }
                if let Some(response) = value.get_mut("response") {
                    binding.rewrite(node, response).await?;
                }
                if let Some(id) = value.get_mut("response_id") {
                    if *id != binding.record.provider_id {
                        return Err(());
                    }
                    *id = binding.id.clone().into();
                }
                resources.rewrite(&mut value).await?;
                crate::protocol::upstream_error::scrub_event(&mut value);
                crate::protocol::ownership::seal(&mut value, &node.ownership, binding.key);
                let wire = axum::body::Bytes::from(format!("event: {kind}\ndata: {value}\n\n"));
                if wire.len() > node.event_bytes {
                    return Err(());
                }
                tokio::time::timeout(node.stream_idle, tx.send(Ok(wire)))
                    .await
                    .map_err(|_| ())?
                    .map_err(|_| ())?;
                if matches!(
                    kind.as_str(),
                    "response.completed"
                        | "response.failed"
                        | "response.incomplete"
                        | "response.cancelled"
                        | "error"
                ) {
                    return Ok(());
                }
            }
        }
    }
}

pub(super) fn resume(
    stream: super::tenancy::TenantPermit,
    node: Arc<Node>,
    binding: ReadBinding,
    upstream: reqwest::Response,
    cursor: Option<i64>,
    permit: super::tenancy::Admitted,
) -> axum::response::Response {
    let (tx, rx) = tokio::sync::mpsc::channel(2);
    let tasks = node.tasks.clone();
    tasks.spawn(async move {
        let _permit = (permit, stream);
        let replayed = tokio::time::timeout(
            node.stream_lifetime,
            replay(&node, &binding, upstream, cursor, &tx),
        )
        .await;
        if !matches!(replayed, Ok(Ok(()))) {
            let _ = tokio::time::timeout(
                node.stream_idle,
                tx.send(Err(std::io::Error::other(
                    "stored response stream interrupted",
                ))),
            )
            .await;
        }
    });
    let stream =
        futures_util::stream::unfold(rx, |mut rx| async move { rx.recv().await.map(|v| (v, rx)) });
    axum::response::Response::builder()
        .header(http::header::CONTENT_TYPE, "text/event-stream")
        .header(http::header::CACHE_CONTROL, "no-cache")
        .body(axum::body::Body::from_stream(stream))
        .expect("SSE response headers")
}
