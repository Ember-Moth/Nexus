use super::{Channel, Failure, Response, Trace, lifecycle::Run};
use crate::{
    metrics::{self, Transport, UpstreamTimer},
    protocol::{
        Endpoint,
        convert::{Exchange, stream::Stream},
        sse::{Decoder, Event},
        stream::{Completion, Observer},
    },
    server::Node,
};
use axum::body::Bytes;
use serde_json::Value;
use std::{io, sync::Arc, time::Duration};
use tokio::{
    sync::{mpsc, oneshot},
    time::timeout,
};

fn encode(event: Event) -> Bytes {
    let mut wire = String::new();
    if let Some(name) = event.name {
        wire.push_str("event: ");
        wire.push_str(&name);
        wire.push('\n');
    }
    for line in event.data.split('\n') {
        wire.push_str("data: ");
        wire.push_str(line);
        wire.push('\n');
    }
    wire.push('\n');
    wire.into()
}

async fn send(
    tx: &mpsc::Sender<Result<Bytes, io::Error>>,
    bytes: Bytes,
    idle: Duration,
) -> Result<(), ()> {
    timeout(idle, tx.send(Ok(bytes)))
        .await
        .map_err(|_| ())?
        .map_err(|_| ())
}

struct Downstream {
    search_metered: crate::protocol::hosted::SearchMeters,
    conversation: Option<crate::protocol::request::ConversationId>,
    resources: Option<crate::control::resources::ResourceScope>,
    stored: Option<crate::control::stored::StoredStream>,
    chat: Option<crate::protocol::chat::Stream>,
    stop: Option<crate::protocol::stop::Preview>,
    pending: Option<(Channel, Response)>,
    tx: mpsc::Sender<Result<Bytes, io::Error>>,
    received: oneshot::Receiver<()>,
}
impl Downstream {
    fn new(
        sender: Channel,
        chat: Option<crate::protocol::chat::Stream>,
        policy: &crate::protocol::approximation::Delivery,
        limit: usize,
    ) -> Self {
        let (tx, rx) = mpsc::channel(2);
        let (receipt, received) = oneshot::channel();
        Self {
            search_metered: Default::default(),
            resources: None,
            stored: None,
            conversation: None,
            chat,
            stop: policy
                .stops
                .clone()
                .map(|s| crate::protocol::stop::Preview::new(s, limit)),
            pending: Some((
                sender,
                Response::Stream {
                    body: rx,
                    receipt,
                    applied: policy.applied,
                },
            )),
            tx,
            received,
        }
    }
    async fn closed(&mut self) {
        match &mut self.pending {
            Some((sender, _)) => sender.closed().await,
            None => self.tx.closed().await,
        }
    }
    async fn parsed_event(
        &mut self,
        value: &serde_json::Value,
        mut original: Option<Event>,
        limit: usize,
        idle: Duration,
    ) -> Result<(), ()> {
        let scrubbed;
        let value = if matches!(value["type"].as_str(), Some("error" | "response.failed")) {
            let mut copy = value.clone();
            crate::protocol::upstream_error::scrub_event(&mut copy);
            if copy != *value {
                original = None;
            }
            scrubbed = copy;
            &scrubbed
        } else {
            value
        };
        let matched = self.stop.as_ref().and_then(|s| s.matched.as_ref());
        let mut projected;
        let value = if value["type"] == "message_delta"
            && let Some(marker) = matched
        {
            projected = value.clone();
            projected["delta"]["stop_reason"] = "stop_sequence".into();
            projected["delta"]["stop_sequence"] = marker.clone().into();
            &projected
        } else {
            value
        };
        if let Some(chat) = &mut self.chat {
            let frames = chat
                .frames_with_stop(value, matched.is_some())
                .map_err(|_| ())?;
            for frame in frames {
                if frame.len() > limit {
                    return Err(());
                }
                self.event(frame.into(), idle).await?;
            }
        } else {
            let frame = encode(match original {
                Some(e) => e,
                None => Event {
                    name: Some(value["type"].as_str().ok_or(())?.into()),
                    data: value.to_string(),
                },
            });
            if frame.len() > limit {
                return Err(());
            }
            self.event(frame, idle).await?;
        }
        Ok(())
    }
    async fn event(&mut self, bytes: Bytes, idle: Duration) -> Result<(), ()> {
        if let Some((sender, response)) = self.pending.take() {
            sender.send(Ok(response)).map_err(|_| ())?;
        }
        send(&self.tx, bytes, idle).await
    }
}
async fn pump(
    mut upstream: reqwest::Response,
    exchange: Exchange<'_>,
    downstream: &mut Downstream,
    node: &Node,
    done: &mut Option<Completion>,
    mut converter: Option<Stream>,
    mut chat_source: Option<crate::protocol::chat_upstream::Stream<'_>>,
) -> Result<(), ()> {
    let (limit, idle) = (node.event_bytes, node.stream_idle);
    let ep = exchange.upstream;

    let mut decoder = Decoder::new(limit);
    let mut observer = Observer::with_search(ep, downstream.search_metered);
    let mut tools = crate::protocol::tools::ToolLimits::new(ep, limit);
    loop {
        let awaiting_id = downstream
            .stored
            .as_ref()
            .is_some_and(|s| s.awaiting_registration());
        let chunk = tokio::select! {
            _=downstream.closed(),if !awaiting_id=>return Err(()),
            result=timeout(idle,upstream.chunk())=>result.map_err(|_|())?.map_err(|_|())?,
        };
        let Some(chunk) = chunk else { return Err(()) };
        // Feed in bounded slices even if a decompressor returns a large block.
        for part in chunk.chunks(8192) {
            let mut events = Vec::new();
            decoder
                .feed(part, |e| {
                    events.push(e);
                    Ok(())
                })
                .map_err(|_| ())?;
            for e in events {
                if let Some(chat) = &mut chat_source {
                    let (frames, terminal) = chat.event(&e.data).map_err(|_| ())?;
                    *done = terminal;
                    for v in frames {
                        downstream.parsed_event(&v, None, limit, idle).await?;
                    }
                    if done.is_some() {
                        return Ok(());
                    }
                    continue;
                }
                let mut value: Value = serde_json::from_str(&e.data).map_err(|_| ())?;
                if awaiting_id && value["type"] == "keepalive" {
                    continue;
                }
                tools.observe(&value).map_err(|_| ())?;
                *done = observer
                    .observe(e.name.as_deref(), &value)
                    .map_err(|_| ())?;
                let mut rewritten = if let Some(scope) = &mut downstream.resources {
                    scope.rewrite(&mut value).await?
                } else {
                    false
                };
                if let Some(stop) = &mut downstream.stop {
                    if !stop.event(&mut value).map_err(|_| ())? {
                        continue;
                    }
                    rewritten = true;
                }
                if let (Some(conv), Some(response)) =
                    (&downstream.conversation, value.get_mut("response"))
                {
                    conv.rewrite(response).map_err(|_| ())?;
                    rewritten = true;
                }
                if let Some(stored) = &mut downstream.stored {
                    stored.rewrite(&mut value).await?;
                    downstream.parsed_event(&value, None, limit, idle).await?;
                } else if let Some(converter) = converter.as_mut() {
                    let events = converter.event(&value, done.as_ref()).map_err(|_| ())?;
                    for event in events {
                        downstream.parsed_event(&event, None, limit, idle).await?;
                    }
                } else {
                    downstream
                        .parsed_event(&value, (!rewritten).then_some(e), limit, idle)
                        .await?;
                }
                if done.is_some() {
                    return Ok(());
                }
            }
        }
    }
}

#[allow(clippy::too_many_arguments)]
pub async fn forward(
    node: Arc<Node>,
    mut run: Run,
    dep: &crate::control::snapshot::Deployment,
    exchange: Exchange<'_>,
    response: reqwest::Response,
    sender: Channel,
    timer: UpstreamTimer,
    chat: Option<bool>,
    request: &Value,
    store: bool,
    conversation: Option<crate::protocol::request::ConversationId>,
    search_metered: crate::protocol::hosted::SearchMeters,
    guardrail: bool,
    background: bool,
    resources: Option<crate::control::resources::ResourceScope>,
) {
    let ep = exchange.upstream;
    let bedrock = dep.is_bedrock();
    let trace = Trace {
        request: run.reservation.id,
        deployment: dep.id,
        endpoint: exchange.client,
        transport: Transport::Sse,
    };
    if !response.status().is_success() {
        let rid = run.reservation.id.to_string();
        let result = run
            .reservation
            .guarded(timeout(node.stream_idle, async {
                if dep.is_chat() {
                    crate::protocol::chat_upstream::buffered(
                        response,
                        exchange.public_model,
                        request,
                        node.upstream.response_limit(),
                    )
                    .await
                } else if bedrock && !dep.invoke() {
                    crate::bedrock::transport::buffered(
                        response,
                        exchange.public_model,
                        &rid,
                        node.upstream.response_limit(),
                        guardrail,
                    )
                    .await
                } else {
                    node.upstream
                        .buffered(
                            response,
                            ep,
                            crate::protocol::request::Operation::Generate,
                            search_metered,
                        )
                        .await
                }
            }))
            .await;
        drop(timer);
        if let Ok(Ok(Ok(reply))) = result {
            super::deliver(
                run,
                sender,
                reply,
                exchange,
                node.upstream.response_limit(),
                trace,
                chat.is_some(),
            )
            .await;
        } else {
            trace.rejected("unreadable_error_response");
            let _ = sender.send(Err(Failure::upstream()));
            run.ended(None, false).await;
        }
        return;
    }
    if !response
        .headers()
        .get("content-type")
        .and_then(|v| v.to_str().ok())
        .is_some_and(|v| {
            v.split(';').next().is_some_and(|s| {
                s.trim().eq_ignore_ascii_case(if bedrock {
                    "application/vnd.amazon.eventstream"
                } else {
                    "text/event-stream"
                })
            })
        })
    {
        trace.rejected("unexpected_content_type");
        let _ = sender.send(Err(Failure::upstream()));
        run.ended(None, false).await;
        return;
    }
    let _active = metrics::sse_stream();
    let mut downstream = Downstream::new(
        sender,
        chat.map(|usage| {
            crate::protocol::chat::Stream::new(
                exchange.public_model,
                usage,
                node.upstream.response_limit(),
            )
        }),
        &run.approximation,
        node.upstream.response_limit(),
    );
    downstream.conversation = conversation;
    downstream.search_metered = search_metered;
    downstream.resources = resources;
    if store {
        downstream.stored = Some(run.stored_stream().background(background));
    }
    let mut done = None;
    let converter = exchange.converts().then(|| {
        Stream::new(
            exchange.client,
            exchange.public_model,
            node.upstream.response_limit(),
        )
        .with_history(run.history.clone())
    });
    let rid = run.reservation.id.to_string();
    let result = run
        .reservation
        .guarded(async {
            if bedrock {
                let id = crate::bedrock::transport::request_id(&response).unwrap_or(rid);
                bedrock_pump(
                    response,
                    exchange,
                    &mut downstream,
                    &node,
                    id,
                    dep.invoke(),
                    guardrail,
                    &mut done,
                    converter,
                )
                .await
            } else {
                pump(
                    response,
                    exchange,
                    &mut downstream,
                    &node,
                    &mut done,
                    converter,
                    dep.is_chat().then(|| {
                        crate::protocol::chat_upstream::Stream::new(
                            exchange.public_model,
                            request,
                            node.upstream.response_limit(),
                        )
                    }),
                )
                .await
            }
        })
        .await;
    drop(timer);
    match &result {
        Ok(Ok(())) => {}
        Ok(Err(())) => trace.interrupted(match &downstream.pending {
            Some((sender, _)) => sender.is_closed(),
            None => downstream.tx.is_closed(),
        }),
        Err(_) => trace.lease_lost(),
    }
    // Background submission and delivery have independent lifetimes. A client
    // disconnect leaves the job polling; a terminal frame can settle it immediately.
    let job = downstream.stored.as_ref().and_then(|s| s.submitted_job());
    if let (Some(handle), Some(terminal)) = (job, done.as_ref())
        && terminal.usage.raw_usage.is_some()
    {
        let _ = crate::server::background::finish(&node.db, handle, terminal).await;
    }
    if let Some((sender, _)) = downstream.pending.take() {
        let _ = sender.send(Err(Failure::upstream()));
        if job.is_some() {
            run.background_submitted(done.as_ref().map(|c| &c.usage))
                .await;
        } else {
            run.ended(done, false).await;
        }

        return;
    }
    if !matches!(result, Ok(Ok(()))) {
        let _ = timeout(
            node.stream_idle,
            downstream
                .tx
                .send(Err(io::Error::other("upstream stream interrupted"))),
        )
        .await;
    }
    drop(downstream.tx);
    let written = matches!(result, Ok(Ok(())))
        && matches!(
            run.reservation
                .guarded(timeout(node.stream_idle, downstream.received))
                .await,
            Ok(Ok(Ok(())))
        );
    if job.is_some() {
        run.background_submitted(done.as_ref().map(|c| &c.usage))
            .await;
    } else {
        run.ended(done, written).await;
    }
}

#[allow(clippy::too_many_arguments)]
async fn bedrock_pump(
    mut response: reqwest::Response,
    exchange: Exchange<'_>,
    downstream: &mut Downstream,
    node: &Node,
    id: String,
    invoke: bool,
    guardrail: bool,
    done: &mut Option<Completion>,
    mut converter: Option<Stream>,
) -> Result<(), ()> {
    use crate::bedrock::stream::{Conversation, Decoder};
    let mut decoder = Decoder::default();
    let mut observer = Observer::new(Endpoint::AnthropicMessages);
    let mut tools =
        crate::protocol::tools::ToolLimits::new(Endpoint::AnthropicMessages, node.event_bytes);
    let mut state = Conversation::new(id, exchange.public_model.into(), node.event_bytes);
    state.guardrail = guardrail;
    loop {
        let chunk=tokio::select!{
            _=downstream.closed()=>return Err(()),
            result=timeout(node.stream_idle,response.chunk())=>result.map_err(|_|())?.map_err(|_|())?,
        }.ok_or(())?;
        let mut bytes = &chunk[..];
        while !bytes.is_empty() {
            let Some((kind, body, exception)) =
                decoder.next(&mut bytes, node.event_bytes).map_err(|_| ())?
            else {
                break;
            };
            let (events, terminal) = if invoke {
                use base64::{Engine, engine::general_purpose::STANDARD};
                let event = if exception {
                    serde_json::json!({"type":"error","error":{"type":"api_error","message":"Bedrock invocation failed"}})
                } else {
                    if kind != "chunk" {
                        return Err(());
                    }
                    let raw = STANDARD
                        .decode(body["bytes"].as_str().ok_or(())?)
                        .map_err(|_| ())?;
                    serde_json::from_slice::<Value>(&raw).map_err(|_| ())?
                };
                tools.observe(&event).map_err(|_| ())?;
                let terminal = observer.observe(None, &event).map_err(|_| ())?;
                (vec![event], terminal)
            } else {
                state.event(&kind, &body, exception).map_err(|_| ())?
            };
            *done = terminal;
            for event in events {
                let translated = match converter.as_mut() {
                    Some(conv) => conv.event(&event, done.as_ref()).map_err(|_| ())?,
                    None => vec![event],
                };
                for v in translated {
                    downstream
                        .parsed_event(&v, None, node.event_bytes, node.stream_idle)
                        .await?;
                }
            }
            if done.is_some() {
                return Ok(());
            }
        }
    }
}
