//! A downstream connection owns exactly one serial upstream conversation.
use super::{Node, error};
use crate::protocol::request::Generate;
use crate::{
    control::snapshot::Binding,
    metrics::{Outcome, RequestTally, Transport, UpstreamTimer},
    protocol::{
        Endpoint,
        request::Route,
        stream::{Completion, Observer, Terminal},
        websocket::{ClientEvent, parse_event, validate_upgrade},
    },
    request::{Failure, lifecycle::Reservation},
    upstream::UpstreamSocket,
};
use axum::{
    Extension,
    extract::{
        State,
        ws::{Message, WebSocket, WebSocketUpgrade},
    },
    response::{IntoResponse, Response},
};
use futures_util::{
    SinkExt, StreamExt,
    stream::{SplitSink, SplitStream},
};
use http::{HeaderMap, Method, StatusCode};
use serde_json::{Value, json};
use std::{
    collections::HashMap,
    sync::{Arc, Mutex},
    time::Duration,
};
use tokio::{
    sync::{Semaphore, mpsc, oneshot},
    time::{Instant, timeout},
};
use tokio_tungstenite::tungstenite::Message as Up;
use tracing::{debug, info, warn};
use uuid::Uuid;
use zeroize::Zeroizing;

const EP: Endpoint = Endpoint::OpenaiResponses;

pub(super) struct Sessions {
    slots: Arc<Semaphore>,
    total: usize,
    tenants: Mutex<HashMap<Uuid, usize>>,
    per_tenant: usize,
}
impl Sessions {
    pub fn new(total: usize, per_tenant: usize) -> Self {
        Self {
            slots: Arc::new(Semaphore::new(total)),
            total,
            tenants: Mutex::new(HashMap::new()),
            per_tenant,
        }
    }
    pub fn in_use(&self) -> usize {
        self.total.saturating_sub(self.slots.available_permits())
    }
    fn enter(self: &Arc<Self>, org: Uuid) -> Option<SessionPermit> {
        let permit = self.slots.clone().try_acquire_owned().ok()?;
        let mut tenants = self.tenants.lock().unwrap();
        let count = tenants.entry(org).or_default();
        if *count >= self.per_tenant {
            return None;
        }
        *count += 1;
        Some(SessionPermit {
            pool: self.clone(),
            org,
            _slot: permit,
        })
    }
}
struct SessionPermit {
    pool: Arc<Sessions>,
    org: Uuid,
    _slot: tokio::sync::OwnedSemaphorePermit,
}
impl Drop for SessionPermit {
    fn drop(&mut self) {
        let mut tenants = self.pool.tenants.lock().unwrap();
        if let Some(n) = tenants.get_mut(&self.org) {
            *n -= 1;
            if *n == 0 {
                tenants.remove(&self.org);
            }
        }
    }
}

pub(super) async fn upgrade(
    State(node): State<Arc<Node>>,
    Extension(stop): Extension<tokio_util::sync::CancellationToken>,
    Extension(session_id): Extension<Uuid>,
    headers: HeaderMap,
    ws: Result<WebSocketUpgrade, axum::extract::ws::rejection::WebSocketUpgradeRejection>,
) -> Response {
    let Ok(cache_key) = crate::protocol::request::cache_key_header(&headers) else {
        return error(
            session_id,
            EP,
            Failure(StatusCode::BAD_REQUEST, "invalid_cache_key"),
        );
    };
    let Ok(raw) = Route::ResponsesWebSocket.key(&headers) else {
        return error(
            session_id,
            EP,
            Failure(StatusCode::UNAUTHORIZED, "invalid_api_key"),
        );
    };
    let Some(index) = node.current() else {
        return error(session_id, EP, Failure::unavailable());
    };
    let Ok(key) = index.authenticate(raw) else {
        return error(
            session_id,
            EP,
            Failure(StatusCode::UNAUTHORIZED, "invalid_api_key"),
        );
    };
    if validate_upgrade(&Method::GET, &headers).is_err() {
        return error(
            session_id,
            EP,
            Failure(StatusCode::BAD_REQUEST, "invalid_upgrade"),
        );
    }
    let Ok(ws) = ws else {
        return error(
            session_id,
            EP,
            Failure(StatusCode::BAD_REQUEST, "invalid_upgrade"),
        );
    };
    let Some(slot) = node.sessions.enter(key.organization_id) else {
        return error(
            session_id,
            EP,
            Failure(StatusCode::TOO_MANY_REQUESTS, "session_limit"),
        );
    };
    if stop.is_cancelled() {
        return error(session_id, EP, Failure::unavailable());
    }
    let raw = Zeroizing::new(raw.to_owned());
    let limit = node.event_bytes;
    // Register before returning 101, so drain also owns upgrades which have not completed yet.
    let (tx, rx) = oneshot::channel();
    node.tasks.clone().spawn(async move {
        let _slot = slot;
        if let Ok(Ok(socket)) = timeout(Duration::from_secs(10), rx).await {
            info!(session_id = %session_id, "websocket session opened");
            session(node, socket, raw, stop, session_id, cache_key).await;
        }
    });
    ws.max_message_size(limit)
        .max_frame_size(limit)
        .write_buffer_size(0)
        .max_write_buffer_size(limit + 1024)
        .on_upgrade(move |socket| async move {
            let _ = tx.send(socket);
        })
        .into_response()
}

type Writer = SplitSink<WebSocket, Message>;
type Reader = SplitStream<WebSocket>;
struct Frame {
    message: Message,
    written: oneshot::Sender<()>,
    terminal: bool,
}
type Output = mpsc::Sender<Frame>;

async fn write(sink: &mut Writer, msg: Message, idle: Duration) -> Result<(), ()> {
    timeout(idle, sink.send(msg))
        .await
        .map_err(|_| ())?
        .map_err(|_| ())
}
async fn emit(out: &Output, value: Value) -> Result<(), ()> {
    let (tx, rx) = oneshot::channel();
    let terminal = matches!(
        value["type"].as_str(),
        Some(
            "response.completed" | "response.incomplete" | "response.cancelled" | "response.failed"
        )
    );
    out.send(Frame {
        terminal,
        message: Message::Text(value.to_string().into()),
        written: tx,
    })
    .await
    .map_err(|_| ())?;
    rx.await.map_err(|_| ())
}
fn failure(code: &str, event: Option<&str>, lane: Option<&str>) -> Value {
    let mut v =
        json!({"type":"error","error":{"type":"invalid_request_error","code":code,"message":code}});
    if let Some(id) = event {
        v["error"]["event_id"] = id.into();
    }
    if let Some(id) = lane {
        v["stream_id"] = id.into();
    }
    v
}
async fn reject(
    sink: &mut Writer,
    code: &str,
    event: Option<&str>,
    lane: Option<&str>,
    idle: Duration,
) -> Result<(), ()> {
    write(
        sink,
        Message::Text(failure(code, event, lane).to_string().into()),
        idle,
    )
    .await
}
#[derive(Default)]
struct Conversation {
    retired: std::collections::VecDeque<String>,
    socket: Option<UpstreamSocket>,
    binding: Option<Binding>,
    parent: Option<String>,
    lane: Option<String>,
}

async fn session(
    node: Arc<Node>,
    ws: WebSocket,
    raw: Zeroizing<String>,
    stop: tokio_util::sync::CancellationToken,
    session_id: Uuid,
    cache_key: Option<String>,
) {
    let (mut sink, mut source) = ws.split();
    let mut state = Conversation::default();
    let opened = Instant::now();
    let mut turns = 0u64;
    let lifetime = opened + Duration::from_secs(3600);
    loop {
        let message = tokio::select! {biased;
            _=stop.cancelled()=>break,
            _=tokio::time::sleep_until(lifetime)=>break,
            m=timeout(Duration::from_secs(120),source.next())=>match m{Ok(Some(Ok(m)))=>m,_=>break}
        };
        let text = match message {
            Message::Text(s) => s,
            Message::Ping(p) => {
                if write(&mut sink, Message::Pong(p), node.stream_idle)
                    .await
                    .is_err()
                {
                    break;
                }
                continue;
            }
            Message::Pong(_) => continue,
            _ => break,
        };
        let event = match parse_event(text.as_bytes(), node.event_bytes) {
            Ok(e) => e,
            Err(_) => {
                if reject(&mut sink, "invalid_event", None, None, node.stream_idle)
                    .await
                    .is_err()
                {
                    break;
                }
                continue;
            }
        };
        let ClientEvent::Create {
            event_id,
            stream_id,
            mut request,
        } = event
        else {
            if let ClientEvent::Cancel {
                event_id,
                stream_id,
                ..
            } = event
                && reject(
                    &mut sink,
                    "no_active_response",
                    event_id.as_deref(),
                    stream_id.as_deref(),
                    node.stream_idle,
                )
                .await
                .is_err()
            {
                break;
            }
            continue;
        };
        request.set_affinity_key(cache_key.clone());
        // Every turn that ends before its attempt starts counts as rejected.
        let mut tally = RequestTally::new(EP, Transport::Websocket);
        let Some(index) = node.current() else {
            let _ = reject(
                &mut sink,
                "dependency_unavailable",
                event_id.as_deref(),
                stream_id.as_deref(),
                node.stream_idle,
            )
            .await;
            break;
        };
        let resources = async {
            super::containers::bind(&node, &index, &raw, &mut request).await?;
            super::vectors::bind(&node, &index, &raw, &mut request).await?;
            super::bind_input(&node, &index, &raw, &mut request).await?;
            super::conversations::bind(&node, &index, &raw, &mut request).await
        }
        .await;
        if let Err(failure) = resources {
            if reject(
                &mut sink,
                failure.1,
                event_id.as_deref(),
                stream_id.as_deref(),
                node.stream_idle,
            )
            .await
            .is_err()
            {
                break;
            }
            continue;
        }
        let mut owned_parent = false;
        if let Some(handle) = request.previous_response_id().and_then(Generate::handle) {
            match crate::control::stored::bind_parent(&node, &index, &raw, &mut request, handle)
                .await
            {
                Ok(()) => {
                    owned_parent = true;
                }
                Err(failure) => {
                    if reject(
                        &mut sink,
                        failure.1,
                        event_id.as_deref(),
                        stream_id.as_deref(),
                        node.stream_idle,
                    )
                    .await
                    .is_err()
                    {
                        break;
                    }
                    continue;
                }
            }
        }
        if !owned_parent
            && request
                .previous_response_id()
                .is_some_and(|p| Some(p) != state.parent.as_deref())
            || state.binding.is_some() && state.lane != stream_id
        {
            if reject(
                &mut sink,
                "previous_response_not_found",
                event_id.as_deref(),
                stream_id.as_deref(),
                node.stream_idle,
            )
            .await
            .is_err()
            {
                break;
            }
            continue;
        }
        let selected = match index.route(&raw, &request, true, state.binding.as_ref()) {
            Ok(s) => s,
            Err(e) => {
                if reject(
                    &mut sink,
                    &e.to_string(),
                    event_id.as_deref(),
                    stream_id.as_deref(),
                    node.stream_idle,
                )
                .await
                .is_err()
                    || state.binding.is_some()
                {
                    break;
                }
                continue;
            }
        };
        let Ok(permit) = node.capacity.clone().try_acquire_owned() else {
            if reject(
                &mut sink,
                "node_busy",
                event_id.as_deref(),
                stream_id.as_deref(),
                node.stream_idle,
            )
            .await
            .is_err()
            {
                break;
            }
            continue;
        };
        let Some(tenant_permit) = node.turns.enter(selected.key.organization_id) else {
            if reject(
                &mut sink,
                "tenant_busy",
                event_id.as_deref(),
                stream_id.as_deref(),
                node.stream_idle,
            )
            .await
            .is_err()
            {
                break;
            }
            continue;
        };
        tally.deployment = Some(selected.deployment.id);
        turns += 1;
        let (tx, rx) = mpsc::channel(2);
        let (cancel, controls) = mpsc::channel(1);
        let request_id = Uuid::new_v4();
        let mut channel = TurnChannel {
            resources: crate::control::resources::ResourceScope::new(
                &node.db,
                selected.key.id,
                index.revision,
                selected.model,
                selected.deployment.id,
            )
            .interpreter(request.interpreter.is_some().then_some(request_id)),
            conversation: request.conversation.clone(),
            out: tx,
            controls,
            disconnected: tokio_util::sync::CancellationToken::new(),
            event: event_id,
            lane: stream_id.clone(),
            idle: node.stream_idle,
            limit: node.event_bytes,
        };
        let disconnected = channel.disconnected.clone();
        let turn = async {
            let _tenant = tenant_permit;
            let started = Instant::now();
            let mut frame = selected.body.clone();
            frame["type"] = "response.create".into();
            if let Some(lane) = stream_id.as_deref() {
                frame["stream_id"] = lane.into();
            }
            let (mut r, secret) =
                match Reservation::reserve(request_id, node.clone(), &index, &selected, EP).await {
                    Ok(v) => v,
                    Err(e) => {
                        channel.error(e.1).await;
                        return false;
                    }
                };
            r.tally = tally;
            debug!(
                session_id = %session_id,
                request_id = %r.id,
                reused = state.socket.is_some(),
                "websocket turn started"
            );
            if state.socket.is_none() {
                let dial=r.guarded(async{tokio::select!{_=channel.disconnected.cancelled()=>Err(()),v=node.upstream.websocket(&selected.deployment.base_url,&secret,node.event_bytes)=>v}}).await;
                match dial {
                    Ok(Ok(socket)) => {
                        debug!(
                            session_id = %session_id,
                            request_id = %r.id,
                            "upstream websocket connected"
                        );
                        state.socket = Some(socket)
                    }
                    _ => {
                        if channel.disconnected.is_cancelled() {
                            r.tally.record(Outcome::ClientCancelled);
                        } else {
                            warn!(
                                session_id = %session_id,
                                request_id = %r.id,
                                deployment_id = %selected.deployment.id,
                                "upstream websocket handshake failed"
                            );
                            r.tally.record(Outcome::UpstreamFailed);
                        }
                        r.abort().await;
                        channel.error("upstream_handshake_failed").await;
                        return false;
                    }
                }
            }
            let request_id = r.id;
            let keep = execute(
                r,
                &mut state,
                &mut channel,
                frame,
                selected.binding(),
                selected.cancellation(),
                permit,
            )
            .await;
            info!(
                session_id = %session_id,
                request_id = %request_id,
                elapsed_ms = started.elapsed().as_millis() as u64,
                reusable = keep,
                "websocket turn ended"
            );
            keep
        };
        let io = drive(
            &mut sink,
            &mut source,
            rx,
            cancel,
            node.stream_idle,
            node.event_bytes,
        );
        tokio::pin!(turn);
        tokio::pin!(io);
        let keep = tokio::select! {
            result = &mut turn => result,
            _ = &mut io => {
                disconnected.cancel();
                turn.await;
                false
            },
            _ = tokio::time::sleep_until(lifetime) => {
                disconnected.cancel();
                turn.await;
                false
            },
            _ = stop.cancelled() => {
                tokio::select! {
                    result = &mut turn => result,
                    _ = &mut io => { disconnected.cancel(); turn.await; false },
                    _ = tokio::time::sleep(Duration::from_secs(15)) => { disconnected.cancel(); turn.await; false },
                }
            }
        };
        if !keep || stop.is_cancelled() {
            break;
        }
    }
    let _ = write(&mut sink, Message::Close(None), node.stream_idle).await;
    info!(
        session_id = %session_id,
        turns,
        elapsed_ms = opened.elapsed().as_millis() as u64,
        "websocket session closed"
    );
}

async fn drive(
    sink: &mut Writer,
    source: &mut Reader,
    mut frames: mpsc::Receiver<Frame>,
    cancel: mpsc::Sender<ClientEvent>,
    idle: Duration,
    limit: usize,
) {
    let mut terminal = false;
    loop {
        tokio::select! {
            frame=frames.recv()=>{
                let Some(frame)=frame else{return;};
                if write(sink,frame.message,idle).await.is_err(){return;}
                terminal |= frame.terminal;
                let _=frame.written.send(());
            }
            message=source.next(), if !terminal=>{
                match message{
                    Some(Ok(Message::Text(s)))=>{
                        if handle_busy(sink,&s,&cancel,limit,idle).await.is_err(){return;}
                    }
                    Some(Ok(Message::Ping(p)))=>{
                        if write(sink,Message::Pong(p),idle).await.is_err(){return;}
                    }
                    Some(Ok(Message::Pong(_)))=>{},
                    _=>return,
                }
            }
        }
    }
}

async fn handle_busy(
    sink: &mut Writer,
    text: &str,
    cancel: &mpsc::Sender<ClientEvent>,
    limit: usize,
    idle: Duration,
) -> Result<(), ()> {
    match parse_event(text.as_bytes(), limit) {
        Ok(ClientEvent::Create {
            event_id,
            stream_id,
            ..
        }) => {
            reject(
                sink,
                "response_in_progress",
                event_id.as_deref(),
                stream_id.as_deref(),
                idle,
            )
            .await
        }
        Ok(event @ ClientEvent::Cancel { .. }) => match cancel.try_send(event) {
            Ok(()) => Ok(()),
            Err(err) => match err.into_inner() {
                ClientEvent::Cancel {
                    event_id,
                    stream_id,
                    ..
                } => {
                    reject(
                        sink,
                        "cancel_pending",
                        event_id.as_deref(),
                        stream_id.as_deref(),
                        idle,
                    )
                    .await
                }
                _ => unreachable!("only cancellation commands enter the control queue"),
            },
        },
        Err(_) => reject(sink, "invalid_event", None, None, idle).await,
    }
}

struct TurnChannel {
    resources: crate::control::resources::ResourceScope,
    conversation: Option<crate::protocol::request::ConversationId>,
    out: Output,
    controls: mpsc::Receiver<ClientEvent>,
    disconnected: tokio_util::sync::CancellationToken,
    event: Option<String>,
    lane: Option<String>,
    idle: Duration,
    limit: usize,
}
impl TurnChannel {
    async fn cancelled_before_send(&mut self) -> bool {
        while let Ok(ClientEvent::Cancel {
            event_id,
            stream_id,
            response_id,
        }) = self.controls.try_recv()
        {
            let matched = stream_id == self.lane && response_id.is_none();
            let code = if matched {
                "cancelled_before_send"
            } else {
                "cancel_target_mismatch"
            };
            let _ = timeout(
                self.idle,
                emit(
                    &self.out,
                    failure(code, event_id.as_deref(), stream_id.as_deref()),
                ),
            )
            .await;
            if matched {
                return true;
            }
        }
        false
    }
    async fn error(&self, code: &str) {
        let _ = timeout(
            self.idle,
            emit(
                &self.out,
                failure(code, self.event.as_deref(), self.lane.as_deref()),
            ),
        )
        .await;
    }
}

async fn execute(
    r: Reservation,
    state: &mut Conversation,
    ch: &mut TurnChannel,
    frame: Value,
    binding: Binding,
    cancellable: bool,
    permit: tokio::sync::OwnedSemaphorePermit,
) -> bool {
    if ch.disconnected.is_cancelled() || ch.cancelled_before_send().await {
        r.tally.record(Outcome::ClientCancelled);
        r.abort().await;
        return false;
    }
    let mut run = match r.start().await {
        Ok(v) => v,
        Err(e) => {
            ch.error(e.1).await;
            return false;
        }
    };
    if ch.cancelled_before_send().await || ch.disconnected.is_cancelled() {
        run.not_sent().await;
        return false;
    }
    let socket = state.socket.as_mut().expect("connected before attempt");
    let mut done = None;
    let disconnected = ch.disconnected.clone();
    let request_id = run.reservation.id;
    let mut timer = UpstreamTimer::start(EP, Transport::Websocket);
    let mut stored = if frame["store"] == true {
        Some(run.stored_stream())
    } else {
        None
    };
    let result=run.reservation.guarded(async{
        tokio::select!{biased;_=disconnected.cancelled()=>Err(()),v=roundtrip(socket,ch,frame,&state.retired,cancellable,&mut done,&mut timer,&mut stored)=>v}
    }).await;
    drop(timer);
    let written = matches!(result, Ok(Ok(())));
    if let Some(id) = done.as_ref().and_then(|d| d.response_id.clone()) {
        if state.retired.len() >= 64 {
            state.retired.pop_front();
        }
        state.retired.push_back(id);
    }
    state.parent = done
        .as_ref()
        .filter(|d| written && d.terminal == Terminal::Completed)
        .and_then(|d| d.response_id.clone());
    state.binding = Some(binding);
    state.lane = ch.lane.clone();
    let reusable = written && done.as_ref().is_some_and(|d| d.response_id.is_some());
    if !reusable {
        state.socket.take();
    }
    // Once the terminal frame is flushed, accept the next create while the original
    // request independently completes its durable facts under its concurrency permit.
    match (written, done) {
        (true, Some(done)) => run.finish_background(done, true, permit).await,
        (_, done) => run.ended(done, written).await,
    }

    if !written && !ch.disconnected.is_cancelled() {
        warn!(
            request_id = %request_id,
            endpoint = EP.as_str(),
            transport = Transport::Websocket.as_str(),
            "upstream websocket turn interrupted"
        );
        ch.error("upstream_interrupted").await;
    }
    reusable
}

enum TurnInput {
    Idle,
    Control(Option<ClientEvent>),
    Upstream(Option<Result<Up, tokio_tungstenite::tungstenite::Error>>),
}

#[allow(clippy::too_many_arguments)]
async fn roundtrip(
    ws: &mut UpstreamSocket,
    ch: &mut TurnChannel,
    frame: Value,
    retired: &std::collections::VecDeque<String>,
    cancellable: bool,
    done: &mut Option<Completion>,
    timer: &mut UpstreamTimer,
    stored: &mut Option<crate::control::stored::StoredStream>,
) -> Result<(), ()> {
    timeout(ch.idle, ws.send(Up::Text(frame.to_string().into())))
        .await
        .map_err(|_| ())?
        .map_err(|_| ())?;
    let mut observer =
        Observer::with_search(EP, crate::protocol::hosted::SearchMeters::for_body(&frame));
    let mut tools = crate::protocol::tools::ToolLimits::new(EP, ch.limit);
    let mut active: Option<String> = None;
    let mut cancelling: Option<(String, Option<String>)> = None;
    let mut deadline = Instant::now() + ch.idle;
    loop {
        let input = tokio::select! {
            _=tokio::time::sleep_until(deadline)=>TurnInput::Idle,
            control=ch.controls.recv()=>TurnInput::Control(control),
            upstream=ws.next()=>TurnInput::Upstream(upstream),
        };
        let text = match input {
            TurnInput::Control(Some(ClientEvent::Cancel {
                event_id,
                stream_id,
                response_id,
            })) => {
                if stream_id != ch.lane
                    || response_id.as_deref().is_some_and(|id| {
                        if let Some(stored) = stored.as_ref() {
                            id != stored.public_id() || active.is_none()
                        } else {
                            Some(id) != active.as_deref()
                        }
                    })
                    || cancelling.is_some()
                {
                    emit(
                        &ch.out,
                        failure(
                            "cancel_target_mismatch",
                            event_id.as_deref(),
                            stream_id.as_deref(),
                        ),
                    )
                    .await?;
                } else if !cancellable {
                    emit(
                        &ch.out,
                        failure(
                            "cancel_closes_connection",
                            event_id.as_deref(),
                            stream_id.as_deref(),
                        ),
                    )
                    .await?;
                    return Err(());
                } else {
                    let wire_id = Uuid::new_v4().to_string();
                    let mut cancel = json!({"type":"response.cancel","event_id":wire_id});
                    if let Some(id) = &active {
                        cancel["response_id"] = json!(id);
                    }
                    if let Some(lane) = &ch.lane {
                        cancel["stream_id"] = json!(lane);
                    }
                    cancelling = Some((wire_id, event_id));
                    timeout(ch.idle, ws.send(Up::Text(cancel.to_string().into())))
                        .await
                        .map_err(|_| ())?
                        .map_err(|_| ())?;
                }
                continue;
            }
            TurnInput::Upstream(Some(Ok(Up::Text(s)))) => {
                timer.first_byte();
                s
            }
            TurnInput::Upstream(Some(Ok(Up::Ping(_)))) => {
                timeout(ch.idle, ws.flush())
                    .await
                    .map_err(|_| ())?
                    .map_err(|_| ())?;
                continue;
            }
            TurnInput::Upstream(Some(Ok(Up::Pong(_)))) => continue,
            _ => return Err(()),
        };
        let mut event: Value = serde_json::from_str(&text).map_err(|_| ())?;
        let kind = event["type"].as_str().ok_or(())?;
        if event
            .get("stream_id")
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty())
            != ch.lane.as_deref()
        {
            continue;
        }
        let id = event["response"]["id"]
            .as_str()
            .or(event["response_id"].as_str());
        if id.is_some_and(|id| {
            retired.iter().any(|r| r == id) || active.as_deref().is_some_and(|a| a != id)
        }) {
            continue;
        }
        if kind == "error" {
            if let Some((wire_id, client_id)) = &cancelling
                && (event["error"]["event_id"] == wire_id.as_str()
                    || event["event_id"] == wire_id.as_str())
            {
                event["error"]["event_id"] = json!(client_id);
                emit(&ch.out, event).await?;
                cancelling = None;
                continue;
            }
            if event["error"]["event_id"].is_string() {
                continue;
            }
        } else if active.is_none() {
            // Provider sockets may send connection telemetry before the response starts.
            if kind != "response.created" {
                continue;
            }
            active = Some(id.ok_or(())?.to_owned());
        }
        tools.observe(&event).map_err(|_| ())?;
        *done = observer.observe(None, &event).map_err(|_| ())?;
        ch.resources.rewrite(&mut event).await?;
        if let (Some(conv), Some(response)) = (&ch.conversation, event.get_mut("response")) {
            conv.rewrite(response).map_err(|_| ())?;
        }
        if let Some(stored) = stored.as_mut() {
            stored.rewrite(&mut event).await?;
        }
        timeout(ch.idle, emit(&ch.out, event))
            .await
            .map_err(|_| ())??;
        if done.is_some() {
            return Ok(());
        }
        deadline = Instant::now() + ch.idle;
    }
}
