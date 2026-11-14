use super::{Channel, Failure, Response, lifecycle::Run};
use crate::{
    protocol::{
        convert::{Exchange, stream::Stream},
        sse::{Decoder, Event},
        stream::{Completion, Observer},
    },
    server::Node,
};
use axum::body::Bytes;
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
    pending: Option<(Channel, Response)>,
    tx: mpsc::Sender<Result<Bytes, io::Error>>,
    received: oneshot::Receiver<()>,
}
impl Downstream {
    fn new(sender: Channel) -> Self {
        let (tx, rx) = mpsc::channel(2);
        let (receipt, received) = oneshot::channel();
        Self {
            pending: Some((sender, Response::Stream { body: rx, receipt })),
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
    limit: usize,
    idle: Duration,
    done: &mut Option<Completion>,
    mut converter: Option<Stream>,
) -> Result<(), ()> {
    let ep = exchange.upstream;

    let mut decoder = Decoder::new(limit);
    let mut observer = Observer::new(ep);
    let mut tools = crate::protocol::tools::ToolLimits::new(ep, limit);
    loop {
        let chunk = tokio::select! {
            _=downstream.closed()=>return Err(()),
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
                let value = serde_json::from_str(&e.data).map_err(|_| ())?;
                tools.observe(&value).map_err(|_| ())?;
                *done = observer
                    .observe(e.name.as_deref(), &value)
                    .map_err(|_| ())?;
                if let Some(converter) = converter.as_mut() {
                    let events = converter.event(&value, done.as_ref()).map_err(|_| ())?;
                    for event in events {
                        let name = event["type"].as_str().ok_or(())?.to_owned();
                        let wire = encode(Event {
                            name: Some(name),
                            data: event.to_string(),
                        });
                        if wire.len() > limit {
                            return Err(());
                        }
                        downstream.event(wire, idle).await?;
                    }
                } else {
                    downstream.event(encode(e), idle).await?;
                }
                if done.is_some() {
                    return Ok(());
                }
            }
        }
    }
}

pub async fn forward(
    node: Arc<Node>,
    mut run: Run,
    dep: &crate::control::snapshot::Deployment,
    exchange: Exchange<'_>,
    response: reqwest::Response,
    sender: Channel,
) {
    let ep = exchange.upstream;
    let bedrock = dep.is_bedrock();
    if !response.status().is_success() {
        let rid = run.reservation.id.to_string();
        let result = run
            .reservation
            .guarded(timeout(node.stream_idle, async {
                if bedrock {
                    crate::bedrock::transport::buffered(
                        response,
                        exchange.public_model,
                        &rid,
                        node.upstream.response_limit(),
                    )
                    .await
                } else {
                    node.upstream.buffered(response, ep).await
                }
            }))
            .await;
        if let Ok(Ok(Ok(reply))) = result {
            super::deliver(run, sender, reply, exchange, node.upstream.response_limit()).await;
        } else {
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
        let _ = sender.send(Err(Failure::upstream()));
        run.ended(None, false).await;
        return;
    }
    let mut downstream = Downstream::new(sender);
    let mut done = None;
    let converter = exchange.converts().then(|| {
        Stream::new(
            exchange.client,
            exchange.public_model,
            node.upstream.response_limit(),
        )
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
                    &mut done,
                    converter,
                )
                .await
            } else {
                pump(
                    response,
                    exchange,
                    &mut downstream,
                    node.event_bytes,
                    node.stream_idle,
                    &mut done,
                    converter,
                )
                .await
            }
        })
        .await;
    if let Some((sender, _)) = downstream.pending.take() {
        let _ = sender.send(Err(Failure::upstream()));
        run.ended(done, false).await;
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
    run.ended(done, written).await;
}

async fn bedrock_pump(
    mut response: reqwest::Response,
    exchange: Exchange<'_>,
    downstream: &mut Downstream,
    node: &Node,
    id: String,
    done: &mut Option<Completion>,
    mut converter: Option<Stream>,
) -> Result<(), ()> {
    use crate::bedrock::stream::{Conversation, Decoder};
    let mut decoder = Decoder::default();
    let mut state = Conversation::new(id, exchange.public_model.into(), node.event_bytes);
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
            let (events, terminal) = state.event(&kind, &body, exception).map_err(|_| ())?;
            *done = terminal;
            for event in events {
                let translated = match converter.as_mut() {
                    Some(conv) => conv.event(&event, done.as_ref()).map_err(|_| ())?,
                    None => vec![event],
                };
                for v in translated {
                    let packet = encode(Event {
                        name: Some(v["type"].as_str().ok_or(())?.to_owned()),
                        data: v.to_string(),
                    });
                    if packet.len() > node.event_bytes {
                        return Err(());
                    }
                    downstream.event(packet, node.stream_idle).await?;
                }
            }
            if done.is_some() {
                return Ok(());
            }
        }
    }
}
