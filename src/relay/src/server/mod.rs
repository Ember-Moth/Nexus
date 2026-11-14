mod delivery;
mod websocket;

use crate::{
    control::{
        ControlError,
        database::Gateway,
        limits::Limits,
        snapshot::{CAPABILITIES, Index},
    },
    protocol::{
        Endpoint,
        request::{Generate, Route},
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
use uuid::Uuid;

pub struct NodeLimits {
    pub lease_ms: u32,
    pub capacity: usize,
    pub stream_idle: Duration,
    pub event_bytes: usize,
    pub sessions: usize,
    pub sessions_per_tenant: usize,
    pub turns_per_tenant: usize,
}

pub struct Node {
    pub(crate) db: Gateway,
    pub(crate) limits: Limits,
    pub(crate) private: SecretKey,
    pub(crate) upstream: Upstream,
    pub(crate) lease_ms: u32,
    pub(crate) stream_idle: Duration,
    pub(crate) event_bytes: usize,
    cluster: Uuid,
    healthy: AtomicBool,
    index: RwLock<Option<Arc<Index>>>,
    capacity: Arc<Semaphore>,
    pub(crate) tasks: TaskTracker,
    sessions: Arc<websocket::Sessions>,
    turns: Arc<websocket::Sessions>,
    draining_deadline: CancellationToken,
}

impl Node {
    pub fn new(
        db: Gateway,
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
        {
            return Err(ControlError::Invalid("node limits"));
        }
        Ok(Arc::new(Self {
            db,
            limits,
            private,
            upstream,
            lease_ms,
            cluster,
            stream_idle,
            event_bytes,
            healthy: AtomicBool::new(false),
            index: RwLock::new(None),
            capacity: Arc::new(Semaphore::new(capacity)),
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
    async fn refresh(&self) -> Result<(), ControlError> {
        let mut snapshot = self.db.snapshot(self.cluster, None).await?;
        let current = self.index.read().unwrap().clone();
        if current.as_ref().is_some_and(|old| {
            old.revision > snapshot.revision
                || (old.revision == snapshot.revision && old.digest != snapshot.digest)
                || old.node != snapshot.node_id
        }) {
            return Err(ControlError::Invalid("snapshot regression"));
        }
        if current
            .as_ref()
            .is_none_or(|old| old.digest != snapshot.digest)
            || snapshot.applied_revision != snapshot.revision
        {
            if snapshot
                .required_capabilities
                .iter()
                .any(|s| !CAPABILITIES.contains(&s.as_str()))
            {
                return self
                    .db
                    .acknowledge(&snapshot, CAPABILITIES, Some("unsupported_capability"))
                    .await;
            }
            let index = match Index::build(&mut snapshot) {
                Ok(i) => i,
                Err(e) => {
                    self.db
                        .acknowledge(&snapshot, CAPABILITIES, Some("invalid_resources"))
                        .await?;
                    return Err(e);
                }
            };
            // Validate the cluster key before acknowledging its new envelope version.
            // A live credential check also runs per request, after snapshot installation.
            for reference in index.credentials.values() {
                let verified = self
                    .db
                    .credential(index.revision, reference)
                    .await
                    .and_then(|env| env.open(index.cluster, reference, &self.private));
                if let Err(error) = verified {
                    self.db
                        .acknowledge(&snapshot, CAPABILITIES, Some("credential_unavailable"))
                        .await?;
                    return Err(error);
                }
            }
            self.db.acknowledge(&snapshot, CAPABILITIES, None).await?;
            *self.index.write().unwrap() = Some(Arc::new(index));
        }
        self.limits
            .heartbeat(snapshot.node_id, snapshot.revision)
            .await
            .map_err(|_| ControlError::Invalid("heartbeat"))
    }
}

fn error(endpoint: Endpoint, failure: Failure) -> Response {
    let Failure(status, code) = failure;
    let json = match endpoint {
        Endpoint::AnthropicMessages => json!({"type":"error","error":{"type":code,"message":code}}),
        Endpoint::OpenaiResponses => json!({"error":{"code":code,"type":code,"message":code}}),
    };
    (status, Json(json)).into_response()
}

#[derive(Clone, Copy)]
struct BodyLimit(usize);
#[derive(Clone)]
struct Streaming(CancellationToken);

async fn generate(
    State(node): State<Arc<Node>>,
    Extension(wire): Extension<Delivery>,
    Extension(streaming): Extension<Streaming>,
    Extension(BodyLimit(max_bytes)): Extension<BodyLimit>,
    incoming: Request,
) -> Response {
    let (parts, body) = incoming.into_parts();
    let endpoint = if parts.uri.path() == "/v1/messages" {
        Endpoint::AnthropicMessages
    } else {
        Endpoint::OpenaiResponses
    };
    let Ok(key) = Route::Generate(endpoint).key(&parts.headers) else {
        return error(
            endpoint,
            Failure(StatusCode::UNAUTHORIZED, "invalid_api_key"),
        );
    };
    if !node.healthy.load(Ordering::Acquire) {
        return error(endpoint, Failure::unavailable());
    }
    let Some(index) = node.index.read().unwrap().clone() else {
        return error(endpoint, Failure::unavailable());
    };
    let Ok(permit) = node.capacity.clone().try_acquire_owned() else {
        return error(endpoint, Failure::unavailable());
    };
    let Ok(body) = to_bytes(body, max_bytes).await else {
        return error(
            endpoint,
            Failure(StatusCode::PAYLOAD_TOO_LARGE, "invalid_request_body"),
        );
    };
    let Ok(json) = serde_json::from_slice::<Value>(&body) else {
        return error(endpoint, Failure(StatusCode::BAD_REQUEST, "invalid_json"));
    };
    let Ok(request) = Generate::parse(endpoint, json, false) else {
        return error(
            endpoint,
            Failure(StatusCode::BAD_REQUEST, "invalid_request"),
        );
    };
    if request.stream {
        streaming.0.cancel();
        if parts.version != http::Version::HTTP_11 {
            return error(
                endpoint,
                Failure(
                    StatusCode::HTTP_VERSION_NOT_SUPPORTED,
                    "sse_requires_http_1_1",
                ),
            );
        }
    }
    let (tx, rx) = oneshot::channel();
    let tracker = node.tasks.clone();
    let key = key.to_owned();
    tracker.spawn(async move {
        let _permit = permit;
        request::run(node, index, key, request, tx).await;
    });
    let result = match rx.await {
        Ok(Ok(result)) => result,
        Ok(Err(e)) => return error(endpoint, e),
        Err(_) => return error(endpoint, Failure::unavailable()),
    };
    match result {
        request::Response::Buffered {
            status,
            body,
            receipt,
        } => {
            let length = body.len();
            wire.track(length, receipt);
            Response::builder()
                .status(status)
                .header(header::CONTENT_TYPE, "application/json")
                .header(header::CONTENT_LENGTH, length)
                .body(Body::from(body))
                .expect("validated response metadata")
        }
        request::Response::Stream { body, receipt } => {
            wire.track_stream(receipt);
            let stream = futures_util::stream::unfold(body, |mut rx| async move {
                rx.recv().await.map(|item| (item, rx))
            });
            Response::builder()
                .header(header::CONTENT_TYPE, "text/event-stream")
                .header(header::CACHE_CONTROL, "no-cache")
                .header(header::TRANSFER_ENCODING, "chunked")
                .body(Body::from_stream(stream))
                .expect("SSE response metadata")
        }
    }
}

async fn ready(State(node): State<Arc<Node>>) -> StatusCode {
    if node.healthy.load(Ordering::Acquire) && node.capacity.available_permits() > 0 {
        StatusCode::OK
    } else {
        StatusCode::SERVICE_UNAVAILABLE
    }
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
        .route("/v1/messages", post(generate))
        .route("/v1/responses", post(generate).get(websocket::upgrade))
        .route("/ready", get(ready))
        .layer(Extension(BodyLimit(max_body)))
        .layer(Extension(stop.clone()))
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
            let healthy = refresh.refresh().await.is_ok();
            refresh.healthy.store(healthy, Ordering::Release);
        }
    });
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
            // Each buffered response has one final write receipt on its connection.
            builder
                .keep_alive(false)
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
    sockets.close();
    node.capacity.close();
    node.healthy.store(false, Ordering::Release);
    if timeout(Duration::from_secs(15), sockets.wait())
        .await
        .is_err()
    {
        node.draining_deadline.cancel();
        sockets.wait().await;
    }
    node.tasks.close();
    let _ = timeout(Duration::from_secs(90), node.tasks.wait()).await;
    maintenance.abort();
    let _ = timeout(Duration::from_secs(1), maintenance).await;
    Ok(())
}
