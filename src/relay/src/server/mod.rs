pub(crate) mod background;
mod batches;
mod containers;
mod conversations;
pub(crate) mod delivery;
mod files;
mod history;
mod observe;
mod preflight;
mod purge;
pub mod tenancy;
mod vectors;
mod websocket;

use crate::{
    control::{
        ControlError,
        database::Gateway,
        keys::KeyTable,
        limits::Limits,
        snapshot::{CAPABILITIES, Index},
    },
    metrics::{self, RequestTally, Transport},
    protocol::{
        Endpoint,
        request::{Generate, Route, route},
    },
    request::{self, Failure},
    upstream::Upstream,
};
use axum::{
    Extension, Json, Router,
    body::{Body, to_bytes},
    extract::{Request, State},
    response::{IntoResponse, Response},
    routing::{get, post},
};
use crypto_box::SecretKey;
use delivery::{Delivery, Socket};
use futures_util::StreamExt;
use http::{StatusCode, header};
use hyper_util::{
    rt::{TokioIo, TokioTimer},
    service::TowerToHyperService,
};
use serde_json::{Value, json};
use std::sync::atomic::{AtomicBool, Ordering};
use std::{
    sync::{Arc, RwLock},
    time::Duration,
};
use tokio::{
    net::TcpListener,
    sync::{Semaphore, oneshot},
    time::{sleep, timeout},
};
use tokio_util::{sync::CancellationToken, task::TaskTracker};
use tracing::{info, warn};
use uuid::Uuid;

pub struct NodeLimits {
    pub lease_ms: u32,
    pub capacity: usize,
    pub stream_idle: Duration,
    pub event_bytes: usize,
    pub sessions: usize,
    pub sessions_per_tenant: usize,
    pub turns_per_tenant: usize,
    /// Concurrent HTTP requests one organization may hold on this node. This and the other
    /// per-tenant limits protect one node's capacity; they are not cluster-wide quotas.
    pub requests_per_tenant: usize,
    /// Concurrent resumed response streams one organization may hold on this node.
    pub streams_per_tenant: usize,
    /// How often the node schedules and runs upstream purges.
    pub purge_interval: Duration,
    /// After SIGTERM, how long open client connections (SSE streams, WebSocket turns) may run
    /// before they are closed. Completion reports then get up to 90 more seconds.
    pub drain: Duration,
}

/// Bounds for `NodeLimits::drain`.
pub const DRAIN: std::ops::RangeInclusive<Duration> =
    Duration::from_secs(1)..=Duration::from_secs(3600);

pub struct Node {
    pub(crate) db: Gateway,
    /// One connection of its own for configuration refresh, so requests queued on locks in
    /// the main pool cannot fail the refresh and mark the node unhealthy.
    control: Gateway,
    pub(crate) limits: Limits,
    pub(crate) private: SecretKey,
    pub(crate) history: Arc<crate::protocol::history::HistoryKey>,
    pub(crate) ownership: Arc<crate::protocol::ownership::OwnershipKey>,
    pub(crate) upstream: Upstream,
    pub(crate) lease_ms: u32,
    pub(crate) stream_idle: Duration,
    pub(crate) event_bytes: usize,
    pub(crate) tenant_requests: Arc<tenancy::TenantSlots>,
    pub(crate) tenant_streams: Arc<tenancy::TenantSlots>,
    pub(crate) purge_interval: Duration,
    pub(crate) drain: Duration,
    cluster: Uuid,
    healthy: AtomicBool,
    index: RwLock<Option<Arc<Index>>>,
    capacity: Arc<Semaphore>,
    capacity_limit: usize,
    pub(crate) tasks: TaskTracker,
    sessions: Arc<websocket::Sessions>,
    turns: Arc<websocket::Sessions>,
    draining_deadline: CancellationToken,
}

impl Node {
    pub fn new(
        db: Gateway,
        control: Gateway,
        limits: Limits,
        private: SecretKey,
        upstream: Upstream,
        cluster: Uuid,
        limits_config: NodeLimits,
    ) -> Result<Arc<Self>, ControlError> {
        let NodeLimits {
            lease_ms,
            capacity,
            stream_idle,
            event_bytes,
            sessions,
            sessions_per_tenant,
            turns_per_tenant,
            requests_per_tenant,
            streams_per_tenant,
            purge_interval,
            drain,
        } = limits_config;
        if stream_idle.is_zero()
            || event_bytes == 0
            || event_bytes > usize::MAX / 2
            || sessions == 0
            || sessions > Semaphore::MAX_PERMITS
            || sessions_per_tenant == 0
            || turns_per_tenant == 0
            || !(100..=60_000).contains(&lease_ms)
            || capacity == 0
            || capacity > Semaphore::MAX_PERMITS
            || requests_per_tenant == 0
            || streams_per_tenant == 0
            || !(Duration::from_millis(100)..=Duration::from_secs(86400)).contains(&purge_interval)
            || !DRAIN.contains(&drain)
        {
            return Err(ControlError::Invalid("node limits"));
        }
        Ok(Arc::new(Self {
            db,
            control,
            limits,
            history: Arc::new(crate::protocol::history::HistoryKey::new(&private, cluster)),
            ownership: Arc::new(crate::protocol::ownership::OwnershipKey::new(
                &private, cluster,
            )),
            private,
            upstream,
            lease_ms,
            cluster,
            stream_idle,
            event_bytes,
            tenant_requests: tenancy::TenantSlots::new(requests_per_tenant),
            tenant_streams: tenancy::TenantSlots::new(streams_per_tenant),
            purge_interval,
            drain,
            healthy: AtomicBool::new(false),
            index: RwLock::new(None),
            capacity: Arc::new(Semaphore::new(capacity)),
            capacity_limit: capacity,
            tasks: TaskTracker::new(),
            draining_deadline: CancellationToken::new(),
            turns: Arc::new(websocket::Sessions::new(capacity, turns_per_tenant)),
            sessions: Arc::new(websocket::Sessions::new(sessions, sessions_per_tenant)),
        }))
    }

    fn current(&self) -> Option<Arc<Index>> {
        if !self.healthy.load(Ordering::Acquire) {
            return None;
        }
        self.index.read().unwrap().clone()
    }
    /// Poll the desired revision on the control connection. The snapshot and the key changes
    /// are only read when the revision, its digest or the node's applied revision differ from
    /// what the node serves; key revocations and scope expiry still surface on every poll,
    /// because both change or fail `config_head`.
    async fn refresh(&self) -> Result<(), ControlError> {
        let head = self.control.head(self.cluster).await?;
        let current = self.index.read().unwrap().clone();
        if current.as_ref().is_some_and(|old| {
            old.revision > head.revision
                || (old.revision == head.revision && old.digest != head.digest)
                || old.node != head.node_id
        }) {
            return Err(ControlError::Invalid("snapshot regression"));
        }
        if current.as_ref().is_none_or(|old| old.digest != head.digest)
            || head.applied_revision != head.revision
        {
            let mut snapshot = self.control.snapshot(&head).await?;
            if snapshot
                .required_capabilities
                .iter()
                .any(|s| !CAPABILITIES.contains(&s.as_str()))
            {
                rejected(&snapshot, "unsupported_capability");
                return self
                    .control
                    .acknowledge(&snapshot, CAPABILITIES, Some("unsupported_capability"))
                    .await;
            }
            let keys = self
                .keys(&snapshot, current.as_ref().map(|i| i.keys.clone()))
                .await?;
            let index = match Index::build(&mut snapshot, keys) {
                Ok(i) => i,
                Err(e) => {
                    rejected(&snapshot, "invalid_resources");
                    self.control
                        .acknowledge(&snapshot, CAPABILITIES, Some("invalid_resources"))
                        .await?;
                    return Err(e);
                }
            };
            // Validate the cluster key before acknowledging its new envelope version.
            // A live credential check also runs per request, after snapshot installation.
            for reference in index.credentials.values() {
                let verified = self
                    .control
                    .credential(index.revision, reference)
                    .await
                    .and_then(|env| env.open(index.cluster, reference, &self.private));
                if let Err(error) = verified {
                    rejected(&snapshot, "credential_unavailable");
                    self.control
                        .acknowledge(&snapshot, CAPABILITIES, Some("credential_unavailable"))
                        .await?;
                    return Err(error);
                }
            }
            self.control
                .acknowledge(&snapshot, CAPABILITIES, None)
                .await?;
            info!(
                cluster_id = %self.cluster,
                node_id = %snapshot.node_id,
                revision = snapshot.revision,
                keys = index.keys.len() as u64,
                "configuration snapshot applied"
            );
            *self.index.write().unwrap() = Some(Arc::new(index));
        }
        self.limits
            .heartbeat(head.node_id, head.revision)
            .await
            .map_err(|_| ControlError::Invalid("heartbeat"))
    }

    /// The cluster's keys at the snapshot's key sequence: the previous table when the sequence
    /// did not move, else the previous table plus the net changes since its sequence, else
    /// (no previous table, or a delta that does not reproduce the release's key set) the full
    /// set read from sequence 0.
    async fn keys(
        &self,
        snapshot: &crate::control::database::Snapshot,
        previous: Option<Arc<KeyTable>>,
    ) -> Result<Arc<KeyTable>, ControlError> {
        let set = snapshot
            .key_set
            .as_ref()
            .ok_or(ControlError::Invalid("key set"))?;
        let target = set.sequence()?;
        if let Some(previous) = previous {
            if previous.sequence() == target && previous.len() == set.count {
                return Ok(previous);
            }
            if previous.sequence() < target {
                let changes = self
                    .control
                    .key_changes(snapshot.revision, previous.sequence())
                    .await?;
                let mut table = (*previous).clone();
                drop(previous);
                if table.apply(&changes, target).is_ok() && table.verify(set).is_ok() {
                    return Ok(Arc::new(table));
                }
                warn!(
                    revision = snapshot.revision,
                    "key changes do not reproduce the release's key set; reading all keys"
                );
            }
        }
        let changes = self.control.key_changes(snapshot.revision, 0).await?;
        let mut table = KeyTable::default();
        table.apply(&changes, target)?;
        table.verify(set)?;
        Ok(Arc::new(table))
    }
}

fn rejected(snapshot: &crate::control::database::Snapshot, reason: &'static str) {
    warn!(
        cluster_id = %snapshot.cluster_id,
        node_id = %snapshot.node_id,
        revision = snapshot.revision,
        reason,
        "configuration snapshot rejected"
    );
}

// One response per connection: each buffered response has one final write receipt on its socket.
// hyper's own keep_alive(false) would also append `close` to a 101 response, and Go-based proxies
// (Traefik, Caddy) drop a Connection header that contains `close`, losing the `upgrade` token and
// refusing the WebSocket. So keep-alive stays on in hyper and every other response ends its
// connection with an explicit `Connection: close`.
async fn one_response_per_connection(mut response: Response) -> Response {
    if response.status() != StatusCode::SWITCHING_PROTOCOLS {
        response
            .headers_mut()
            .insert(header::CONNECTION, http::HeaderValue::from_static("close"));
    }
    response
}

async fn correlate(mut req: Request, next: axum::middleware::Next) -> Response {
    let id = Uuid::new_v4();
    req.extensions_mut().insert(id);
    let mut response = next.run(req).await;
    response
        .headers_mut()
        .insert("x-request-id", id.to_string().parse().expect("UUID header"));
    response
}

fn error(id: Uuid, endpoint: Endpoint, failure: Failure) -> Response {
    let Failure(status, code) = failure;
    info!(request_id = %id, endpoint = endpoint.as_str(), reason = code, "relay request rejected");
    let json = match endpoint {
        Endpoint::AnthropicMessages => json!({"type":"error","error":{"type":code,"message":code}}),
        Endpoint::OpenaiResponses => json!({"error":{"code":code,"type":code,"message":code}}),
    };
    (status, Json(json)).into_response()
}

/// How long a tenant may take to send a request body to a resource API.
pub(super) const UPLOAD_DEADLINE: Duration = Duration::from_secs(60);

/// Answer with an upstream reply. Errors are scrubbed into the client's protocol.
pub(super) fn reply(status: StatusCode, value: Value, client: Endpoint) -> Response {
    if status.is_success() {
        return (status, Json(value)).into_response();
    }
    let (status, value) = crate::protocol::upstream_error::scrub(status, &value, client);
    (status, Json(value)).into_response()
}

#[derive(Clone, Copy)]
struct BodyLimit(usize);
#[derive(Clone)]
struct Streaming(CancellationToken);

async fn generate(
    State(node): State<Arc<Node>>,
    Extension(id): Extension<Uuid>,
    Extension(wire): Extension<Delivery>,
    Extension(streaming): Extension<Streaming>,
    Extension(BodyLimit(max_bytes)): Extension<BodyLimit>,
    incoming: Request,
) -> Response {
    let (parts, body) = incoming.into_parts();
    let (route, endpoint) = match route(&parts.method, parts.uri.path()) {
        Some(r @ Route::Generate(ep)) => (r, ep),
        Some(Route::Chat) => (Route::Chat, Endpoint::OpenaiResponses),
        Some(Route::Compact) => (Route::Compact, Endpoint::OpenaiResponses),
        Some(Route::CountTokens) => (Route::CountTokens, Endpoint::AnthropicMessages),
        _ => return StatusCode::NOT_FOUND.into_response(),
    };
    // Every early return below counts as a rejected request.
    let mut tally = if route == Route::CountTokens {
        RequestTally::detached()
    } else {
        RequestTally::new(endpoint, Transport::Http)
    };
    let Ok(key) = route.key(&parts.headers) else {
        return error(
            id,
            endpoint,
            Failure(StatusCode::UNAUTHORIZED, "invalid_api_key"),
        );
    };
    if !node.healthy.load(Ordering::Acquire) {
        return error(id, endpoint, Failure::unavailable());
    }
    let Some(index) = node.index.read().unwrap().clone() else {
        return error(id, endpoint, Failure::unavailable());
    };
    // An unauthenticated caller is refused during selection; it holds no tenant slot.
    let tenant = match index.authenticate(key) {
        Ok(k) => match node.tenant_requests.enter(k.organization_id) {
            Some(slot) => Some(slot),
            None => return error(id, endpoint, tenancy::tenant_limited()),
        },
        Err(_) => None,
    };
    let Ok(permit) = node.capacity.clone().try_acquire_owned() else {
        return error(id, endpoint, Failure::unavailable());
    };
    let Ok(body) = to_bytes(body, max_bytes).await else {
        return error(
            id,
            endpoint,
            Failure(StatusCode::PAYLOAD_TOO_LARGE, "invalid_request_body"),
        );
    };
    let Ok(json) = serde_json::from_slice::<Value>(&body) else {
        return error(
            id,
            endpoint,
            Failure(StatusCode::BAD_REQUEST, "invalid_json"),
        );
    };
    let parsed = match route {
        Route::Chat => Generate::parse_chat(json),
        Route::Compact => Generate::parse_compact(json),
        Route::CountTokens => Generate::parse_count(json),
        _ => Generate::parse(endpoint, json, false),
    };
    let Ok(mut request) = parsed else {
        return error(
            id,
            endpoint,
            Failure(StatusCode::BAD_REQUEST, "invalid_request"),
        );
    };
    if matches!(route, Route::Generate(_) | Route::Chat) {
        request.allow_approximation =
            match crate::protocol::approximation::Choices::parse(&parts.headers) {
                Ok(choices) => choices,
                Err(_) => {
                    return error(
                        id,
                        endpoint,
                        Failure(StatusCode::BAD_REQUEST, "invalid_approximation"),
                    );
                }
            };
    }
    if endpoint == Endpoint::AnthropicMessages {
        match crate::protocol::request::AnthropicHeaders::parse(&parts.headers) {
            Ok(headers) => request.anthropic = headers,
            Err(_) => {
                return error(
                    id,
                    endpoint,
                    Failure(StatusCode::BAD_REQUEST, "invalid_anthropic_headers"),
                );
            }
        }
    }
    match crate::protocol::request::cache_key_header(&parts.headers) {
        Ok(key) => request.set_affinity_key(key),
        Err(_) => {
            return error(
                id,
                endpoint,
                Failure(StatusCode::BAD_REQUEST, "invalid_cache_key"),
            );
        }
    }
    if let Err(failure) = containers::bind(&node, &index, key, &mut request).await {
        return error(id, endpoint, failure);
    }
    if let Err(failure) = vectors::bind(&node, &index, key, &mut request).await {
        return error(id, endpoint, failure);
    }
    if let Err(failure) = conversations::bind(&node, &index, key, &mut request).await {
        return error(id, endpoint, failure);
    }
    if let Err(failure) = bind_input(&node, &index, key, &mut request).await {
        return error(id, endpoint, failure);
    }
    if route == Route::CountTokens {
        return match preflight::count(&node, &index, key, id, request).await {
            Ok(response) => response,
            Err(failure) => error(id, endpoint, failure),
        };
    }
    if request.stream {
        tally.set_transport(Transport::Sse);
        streaming.0.cancel();
        if parts.version != http::Version::HTTP_11 {
            return error(
                id,
                endpoint,
                Failure(
                    StatusCode::HTTP_VERSION_NOT_SUPPORTED,
                    "sse_requires_http_1_1",
                ),
            );
        }
    }
    // Responses output carries item IDs and ciphertext bound to the calling key.
    let owner = (endpoint == Endpoint::OpenaiResponses && route != Route::Chat)
        .then(|| {
            index
                .authenticate(key)
                .ok()
                .map(|k| (node.ownership.clone(), k.id))
        })
        .flatten();
    let (tx, rx) = oneshot::channel();
    let tracker = node.tasks.clone();
    let key = key.to_owned();
    tracker.spawn(async move {
        let _permit = (permit, tenant);
        request::run(id, node, index, key, request, tx, tally).await;
    });
    let result = match rx.await {
        Ok(Ok(result)) => result,
        Ok(Err(e)) => return error(id, endpoint, e),
        Err(_) => return error(id, endpoint, Failure::unavailable()),
    };
    match result {
        request::Response::Buffered {
            status,
            mut body,
            receipt,
            applied,
        } => {
            let mut status = status;
            if !status.is_success() {
                // Generation errors come from the upstream, possibly converted to this protocol.
                let (scrubbed, value) =
                    crate::protocol::upstream_error::scrub_bytes(status, &body, endpoint);
                status = scrubbed;
                body = value.to_string().into_bytes();
            } else if let Some((owner, kid)) = &owner
                && let Ok(mut value) = serde_json::from_slice::<Value>(&body)
            {
                crate::protocol::ownership::seal(&mut value, owner, *kid);
                body = value.to_string().into_bytes();
            }
            let length = body.len();
            wire.track(length, receipt);
            applied
                .headers(Response::builder())
                .status(status)
                .header(header::CONTENT_TYPE, "application/json")
                .header(header::CONTENT_LENGTH, length)
                .body(Body::from(body))
                .expect("validated response metadata")
        }
        request::Response::Stream {
            body,
            receipt,
            written,
            applied,
        } => {
            wire.track_stream(receipt, written);
            let stream = futures_util::stream::unfold(body, move |mut rx| {
                let owner = owner.clone();
                async move {
                    let item = rx.recv().await?;
                    let item = match (item, owner) {
                        (Ok(frame), Some((owner, kid))) => Ok(seal_frames(&frame, &owner, kid)),
                        (item, _) => item,
                    };
                    Some((item, rx))
                }
            });
            applied
                .headers(Response::builder())
                .header(header::CONTENT_TYPE, "text/event-stream")
                .header(header::CACHE_CONTROL, "no-cache")
                .header(header::TRANSFER_ENCODING, "chunked")
                .body(Body::from_stream(stream))
                .expect("SSE response metadata")
        }
    }
}

/// Seal the JSON data of whole SSE frames. The stream writer emits one complete frame per chunk.
fn seal_frames(
    frames: &axum::body::Bytes,
    owner: &crate::protocol::ownership::OwnershipKey,
    kid: Uuid,
) -> axum::body::Bytes {
    let Ok(text) = std::str::from_utf8(frames) else {
        return frames.clone();
    };
    let mut out = String::with_capacity(text.len() + 64);
    for frame in text.split_inclusive("\n\n") {
        let mut name = None;
        let mut data = Vec::new();
        for line in frame.lines() {
            if let Some(v) = line.strip_prefix("event: ") {
                name = Some(v);
            } else if let Some(v) = line.strip_prefix("data: ") {
                data.push(v);
            }
        }
        match serde_json::from_str::<Value>(&data.join("\n")) {
            Ok(mut value) if frame.ends_with("\n\n") => {
                crate::protocol::ownership::seal(&mut value, owner, kid);
                if let Some(name) = name {
                    out.push_str("event: ");
                    out.push_str(name);
                    out.push('\n');
                }
                out.push_str("data: ");
                out.push_str(&value.to_string());
                out.push_str("\n\n");
            }
            _ => out.push_str(frame),
        }
    }
    out.into()
}

async fn ready(State(node): State<Arc<Node>>) -> StatusCode {
    if node.healthy.load(Ordering::Acquire) && node.capacity.available_permits() > 0 {
        StatusCode::OK
    } else {
        StatusCode::SERVICE_UNAVAILABLE
    }
}

async fn bind_input(
    node: &Node,
    index: &Index,
    key: &str,
    request: &mut Generate,
) -> Result<(), Failure> {
    if request.endpoint == Endpoint::OpenaiResponses {
        let kid = index.authenticate(key)?.id;
        request
            .open_items(&node.ownership, kid)
            .map_err(|_| Failure(StatusCode::BAD_REQUEST, "item_not_owned"))?;
    }
    history::bind(node, index, key, request).await?;
    files::bind(node, index, key, request).await
}

pub async fn serve(
    node: Arc<Node>,
    listener: TcpListener,
    max_body: usize,
    stop: CancellationToken,
) -> Result<(), Box<dyn std::error::Error>> {
    node.refresh().await?;
    node.healthy.store(true, Ordering::Release);
    let router = Router::new()
        .route(
            "/v1/containers",
            get(containers::handle).post(containers::handle),
        )
        .route(
            "/v1/containers/{id}",
            get(containers::handle).delete(containers::handle),
        )
        .route(
            "/v1/containers/{id}/files",
            get(containers::handle).post(containers::handle),
        )
        .route(
            "/v1/containers/{id}/files/{file}",
            get(containers::handle).delete(containers::handle),
        )
        .route(
            "/v1/containers/{id}/files/{file}/content",
            get(containers::handle),
        )
        .route(
            "/v1/vector_stores",
            get(vectors::handle_request).post(vectors::handle_request),
        )
        .route(
            "/v1/vector_stores/{id}",
            get(vectors::handle_request)
                .post(vectors::handle_request)
                .delete(vectors::handle_request),
        )
        .route(
            "/v1/vector_stores/{id}/search",
            post(vectors::handle_request),
        )
        .route(
            "/v1/vector_stores/{id}/files",
            get(vectors::handle_request).post(vectors::handle_request),
        )
        .route(
            "/v1/vector_stores/{id}/files/{file}",
            get(vectors::handle_request)
                .post(vectors::handle_request)
                .delete(vectors::handle_request),
        )
        .route(
            "/v1/vector_stores/{id}/files/{file}/content",
            get(vectors::handle_request),
        )
        .route(
            "/v1/vector_stores/{id}/file_batches",
            post(vectors::handle_request),
        )
        .route(
            "/v1/vector_stores/{id}/file_batches/{batch}",
            get(vectors::handle_request),
        )
        .route(
            "/v1/vector_stores/{id}/file_batches/{batch}/cancel",
            post(vectors::handle_request),
        )
        .route(
            "/v1/vector_stores/{id}/file_batches/{batch}/files",
            get(vectors::handle_request),
        )
        .route("/v1/conversations", post(conversations::handle))
        .route(
            "/v1/conversations/{id}",
            get(conversations::handle)
                .post(conversations::handle)
                .delete(conversations::handle),
        )
        .route(
            "/v1/conversations/{id}/items",
            get(conversations::handle).post(conversations::handle),
        )
        .route(
            "/v1/conversations/{id}/items/{item}",
            get(conversations::handle).delete(conversations::handle),
        )
        .route("/v1/models", get(preflight::models))
        .route(
            "/v1/responses/{handle}/cancel",
            post(preflight::stored_response),
        )
        .route("/v1/files", get(files::collection).post(files::collection))
        .route("/v1/files/{id}", get(files::item).delete(files::item))
        .route("/v1/files/{id}/content", get(files::item))
        .route(
            "/v1/responses/{handle}",
            get(preflight::stored_response).delete(preflight::stored_response),
        )
        .route(
            "/v1/messages/batches",
            get(batches::handle).post(batches::handle),
        )
        .route(
            "/v1/messages/batches/{id}",
            get(batches::handle).delete(batches::handle),
        )
        .route("/v1/messages/batches/{id}/results", get(batches::handle))
        .route("/v1/messages/batches/{id}/cancel", post(batches::handle))
        .route(
            "/v1/responses/{handle}/input_items",
            get(preflight::stored_response),
        )
        .route("/v1/chat/completions", post(generate))
        .route("/v1/messages/count_tokens", post(generate))
        .route("/v1/messages", post(generate))
        .route("/v1/responses/compact", post(generate))
        .route("/v1/responses", post(generate).get(websocket::upgrade))
        .route_layer(axum::middleware::from_fn(correlate))
        .route("/ready", get(ready))
        .route("/live", get(metrics::live))
        .layer(Extension(BodyLimit(max_body)))
        .layer(Extension(stop.clone()))
        .layer(axum::middleware::map_response(one_response_per_connection))
        .with_state(node.clone());
    let refresh = node.clone();
    let refresh_stop = stop.clone();
    let maintenance = tokio::spawn(async move {
        let mut messages = None;
        loop {
            if messages.is_none() {
                messages = timeout(
                    Duration::from_secs(2),
                    refresh.limits.notifications(refresh.cluster),
                )
                .await
                .ok()
                .and_then(Result::ok);
            }
            let delay = Duration::from_millis(2000 + rand::random::<u16>() as u64 % 1000);
            tokio::select! {
                _=refresh_stop.cancelled()=>break,
                _=sleep(delay)=>{},
                message=async{
                    match &mut messages{ Some(stream)=>stream.next().await,None=>std::future::pending().await }
                }=>{if message.is_none(){messages=None;}}
            }
            let result = refresh.refresh().await;
            match &result {
                Err(error) => warn!(reason = error.reason(), "control-plane refresh failed"),
                Ok(()) if !refresh.healthy.load(Ordering::Acquire) => {
                    info!("control-plane refresh recovered")
                }
                Ok(()) => {}
            }
            refresh.healthy.store(result.is_ok(), Ordering::Release);
        }
    });
    let poller = tokio::spawn(background::supervise(node.clone(), stop.clone()));
    let vector_poller = tokio::spawn(vectors::supervise(node.clone(), stop.clone()));
    let batch_poller = tokio::spawn(batches::supervise(node.clone(), stop.clone()));
    let purger = tokio::spawn(purge::supervise(node.clone(), stop.clone()));
    let sockets = TaskTracker::new();
    loop {
        let (stream, _) =
            tokio::select! {biased;_=stop.cancelled()=>break,res=listener.accept()=>res?};
        let wire = Delivery::default();
        let io = TokioIo::new(Socket {
            stream,
            delivery: wire.clone(),
        });
        let streaming = CancellationToken::new();
        let service = TowerToHyperService::new(
            router
                .clone()
                .layer(Extension(wire))
                .layer(Extension(Streaming(streaming.clone()))),
        );
        let abort = node.draining_deadline.clone();
        sockets.spawn(async move {
            let mut builder = hyper::server::conn::http1::Builder::new();
            // `one_response_per_connection` closes every connection after its response.
            builder
                .timer(TokioTimer::new())
                .header_read_timeout(Duration::from_secs(10));
            let connection = builder.serve_connection(io, service).with_upgrades();
            tokio::pin!(connection);
            let exchange = async {
                tokio::select! {
                    _ = &mut connection => return,
                    _ = streaming.cancelled() => {},
                    _ = sleep(Duration::from_secs(180)) => return,
                }
                let _ = connection.await;
            };
            tokio::select! {_=exchange=>{},_=abort.cancelled()=>{}}
        });
    }
    let drain = std::time::Instant::now();
    metrics::set_draining(true);
    info!(in_flight = node.tasks.len(), "drain started");
    sockets.close();
    node.capacity.close();
    node.healthy.store(false, Ordering::Release);
    if timeout(node.drain, sockets.wait()).await.is_err() {
        warn!("drain deadline reached; closing open connections");
        node.draining_deadline.cancel();
        sockets.wait().await;
    }
    node.tasks.close();
    if timeout(Duration::from_secs(90), node.tasks.wait())
        .await
        .is_err()
    {
        warn!(
            in_flight = node.tasks.len(),
            "drain deadline reached; abandoning background tasks"
        );
    }
    maintenance.abort();
    let _ = timeout(Duration::from_secs(1), maintenance).await;
    let _ = poller.await;
    let _ = batch_poller.await;
    let _ = purger.await;
    let _ = vector_poller.await;
    info!(
        elapsed_ms = drain.elapsed().as_millis() as u64,
        "drain finished"
    );
    Ok(())
}
