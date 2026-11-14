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
    base: &url::Url,
    exchange: Exchange<'_>,
    credential: &str,
    body: &Value,
    mut sender: Channel,
) {
    let ep = exchange.upstream;
    let opened=run.reservation.guarded(async{
        tokio::select!{_=sender.closed()=>Err(()),r=node.upstream.open(base,ep,credential,body,true)=>r}
    }).await;
    let Ok(Ok(response)) = opened else {
        let _ = sender.send(Err(Failure::upstream()));
        run.ended(None, false).await;
        return;
    };
    if !response.status().is_success() {
        let result = run
            .reservation
            .guarded(timeout(
                node.stream_idle,
                node.upstream.buffered(response, ep),
            ))
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
            v.split(';')
                .next()
                .is_some_and(|s| s.trim().eq_ignore_ascii_case("text/event-stream"))
        })
    {
        let _ = sender.send(Err(Failure::upstream()));
        run.ended(None, false).await;
        return;
    }
    let mut downstream = Downstream::new(sender);
    let mut done = None;
    let result = run
        .reservation
        .guarded(pump(
            response,
            exchange,
            &mut downstream,
            node.event_bytes,
            node.stream_idle,
            &mut done,
            exchange.converts().then(|| {
                Stream::new(
                    exchange.client,
                    exchange.public_model,
                    node.upstream.response_limit(),
                )
            }),
        ))
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
