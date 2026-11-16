mod completion;
pub(crate) mod lifecycle;
mod sse;

use crate::{
    control::{
        ControlError,
        database::Gateway,
        snapshot::{Index, Rejection, Selection},
    },
    metrics::{
        self, FailoverReason, FailoverResult, Outcome, RequestTally, Transport, UpstreamTimer,
    },
    protocol::{
        Endpoint,
        convert::{self, Exchange},
        request::Generate,
        usage::UsageEvidence,
    },
    server::Node,
    upstream::{DECLINED, ErrorContract, Refusal, Reply, UpstreamError, refusal},
};
use http::StatusCode;
use std::{sync::Arc, time::Duration};
use tokio::{
    sync::{mpsc, oneshot},
    time::{Instant, timeout},
};
use tracing::{info, warn};
use uuid::Uuid;

pub enum Response {
    Buffered {
        status: StatusCode,
        body: Vec<u8>,
        receipt: oneshot::Sender<()>,
        applied: crate::protocol::approximation::Choices,
    },
    Stream {
        body: mpsc::Receiver<Result<axum::body::Bytes, std::io::Error>>,
        receipt: oneshot::Sender<()>,
        /// Bytes the connection wrote to the client; the stream's waits follow its progress.
        written: std::sync::Arc<std::sync::atomic::AtomicU64>,
        /// Set once the stream ended as it should. A body that ends without it was cut short,
        /// and the connection is aborted instead of closing the chunked body cleanly.
        complete: std::sync::Arc<std::sync::atomic::AtomicBool>,
        applied: crate::protocol::approximation::Choices,
    },
}
#[derive(Clone, Copy)]
pub struct Failure(pub StatusCode, pub &'static str);
impl Failure {
    pub fn unavailable() -> Self {
        Self(StatusCode::SERVICE_UNAVAILABLE, "dependency_unavailable")
    }
    pub fn upstream() -> Self {
        Self(StatusCode::BAD_GATEWAY, "upstream_error")
    }
    /// The operator has not enabled a provider feature this request needs on its deployment.
    pub fn feature_disabled() -> Self {
        Self(StatusCode::BAD_REQUEST, "feature_disabled")
    }
}
impl From<Rejection> for Failure {
    fn from(r: Rejection) -> Self {
        match r {
            Rejection::Key => Self(StatusCode::UNAUTHORIZED, "invalid_api_key"),
            Rejection::Forbidden => Self(StatusCode::FORBIDDEN, "model_forbidden"),
            Rejection::Unsupported => Self(StatusCode::BAD_REQUEST, "unsupported_request"),
            Rejection::Unavailable => Self(StatusCode::BAD_REQUEST, "unsupported_deployment"),
            Rejection::FeatureDisabled => Self::feature_disabled(),
        }
    }
}
impl From<ControlError> for Failure {
    fn from(e: ControlError) -> Self {
        if matches!(e, ControlError::Saturated) {
            return Self(StatusCode::TOO_MANY_REQUESTS, "rate_limited");
        }
        match e.database_message() {
            Some("response_pending") => Self(StatusCode::CONFLICT, "response_pending"),
            Some("resource_not_found") => Self(StatusCode::NOT_FOUND, "response_not_found"),
            Some("key_inactive") => Self(StatusCode::UNAUTHORIZED, "invalid_api_key"),
            Some(
                "model_forbidden" | "snapshot_forbidden" | "key_outside_cluster" | "account_frozen",
            ) => Self(StatusCode::FORBIDDEN, "access_denied"),
            Some("insufficient_funds") => Self(StatusCode::PAYMENT_REQUIRED, "insufficient_funds"),
            Some("budget_exceeded") => Self(StatusCode::PAYMENT_REQUIRED, "budget_exceeded"),
            Some("account_not_ready") => Self(StatusCode::CONFLICT, "account_not_ready"),
            Some("request_conflict" | "attempt_conflict") => {
                Self(StatusCode::CONFLICT, "request_conflict")
            }
            Some("request_not_executable") => Self(StatusCode::CONFLICT, "request_not_executable"),
            Some("invalid_bounds" | "invalid_reserve_amount") => {
                Self(StatusCode::BAD_REQUEST, "invalid_bounds")
            }
            Some("price_unavailable" | "cost_unavailable" | "currency_config_mismatch") => Self(
                StatusCode::SERVICE_UNAVAILABLE,
                "billing_configuration_unavailable",
            ),
            Some("deployment_unavailable") => {
                Self(StatusCode::SERVICE_UNAVAILABLE, "deployment_unavailable")
            }
            Some("credential_unavailable") => Self(
                StatusCode::SERVICE_UNAVAILABLE,
                "upstream_credential_unavailable",
            ),
            Some("revision_not_current" | "snapshot_scope_expired" | "snapshot_unavailable") => {
                Self(StatusCode::SERVICE_UNAVAILABLE, "snapshot_unavailable")
            }
            _ => Self::unavailable(),
        }
    }
}

type Channel = oneshot::Sender<Result<Response, Failure>>;

/// Identifiers for logs about one upstream exchange.
#[derive(Clone, Copy)]
struct Trace {
    request: Uuid,
    deployment: Uuid,
    endpoint: Endpoint,
    transport: Transport,
}

impl Trace {
    /// Log why no upstream response arrived. A client that left is logged when the request finishes.
    fn not_answered<T>(self, result: &Result<Option<Result<T, UpstreamError>>, Failure>) {
        match result {
            Ok(Some(Err(error))) => warn!(
                request_id = %self.request,
                deployment_id = %self.deployment,
                endpoint = self.endpoint.as_str(),
                transport = self.transport.as_str(),
                error = %error,
                "upstream request failed"
            ),
            Err(_) => self.lease_lost(),
            Ok(_) => {}
        }
    }
    fn lease_lost(self) {
        warn!(
            request_id = %self.request,
            deployment_id = %self.deployment,
            endpoint = self.endpoint.as_str(),
            transport = self.transport.as_str(),
            reason = "lease_lost",
            "upstream request interrupted"
        );
    }
    fn rejected(self, reason: &'static str) {
        warn!(
            request_id = %self.request,
            deployment_id = %self.deployment,
            endpoint = self.endpoint.as_str(),
            transport = self.transport.as_str(),
            reason,
            "upstream response rejected"
        );
    }
    /// Count and log what followed an attempt that provably did not execute.
    fn failover(
        self,
        attempt: usize,
        next: Option<Uuid>,
        reason: FailoverReason,
        result: FailoverResult,
    ) {
        metrics::failover(reason, result);
        warn!(
            request_id = %self.request,
            attempt = attempt as u64,
            deployment_id = %self.deployment,
            next_deployment_id = next.map(tracing::field::display),
            endpoint = self.endpoint.as_str(),
            transport = self.transport.as_str(),
            reason = reason.as_str(),
            outcome = result.as_str(),
            "upstream attempt declined"
        );
    }
    fn interrupted(self, client_gone: bool) {
        if client_gone {
            info!(
                request_id = %self.request,
                endpoint = self.endpoint.as_str(),
                transport = self.transport.as_str(),
                "client disconnected before the terminal event"
            );
        } else {
            warn!(
                request_id = %self.request,
                deployment_id = %self.deployment,
                endpoint = self.endpoint.as_str(),
                transport = self.transport.as_str(),
                "upstream stream interrupted"
            );
        }
    }
}

async fn cancel(db: &Gateway, rid: Uuid) -> bool {
    for _ in 0..3 {
        match db.cancel_unstarted(rid, "relay_cancelled").await {
            Ok(_) => return true,
            Err(e) if e.retryable() => tokio::time::sleep(Duration::from_millis(100)).await,
            Err(_) => return false,
        }
    }
    false
}

/// How an upstream answered the opening of one attempt.
enum Opened {
    /// The existing delivery path handles this response, including non-retryable errors.
    Answer(reqwest::Response),
    Buffered(Reply),
    /// The upstream provably did not execute the generation; the attempt reports
    /// [`DECLINED`]. A declined status keeps its buffered reply as cost evidence and as the
    /// client's error if no attempt follows.
    Declined(Option<Reply>, FailoverReason),
    /// No usable answer; the generation may have executed or the client has left.
    Lost,
}

// The timer marks the upstream's first byte; it is the only argument added for observability.
#[allow(clippy::too_many_arguments)]
async fn open(
    node: &Node,
    run: &mut lifecycle::Run,
    selected: &Selection<'_>,
    exchange: Exchange<'_>,
    credential: &str,
    request: &Generate,
    sender: &mut Channel,
    timer: &mut UpstreamTimer,
) -> Opened {
    let stream = request.stream;
    let (dep, body) = (selected.deployment, &selected.body);
    let trace = Trace {
        request: run.reservation.id,
        deployment: dep.id,
        endpoint: exchange.client,
        transport: if stream {
            Transport::Sse
        } else {
            Transport::Http
        },
    };
    let ep = exchange.upstream;
    let bedrock = dep.is_bedrock();
    let converted_headers = (exchange.converts() && ep == Endpoint::AnthropicMessages)
        .then(|| crate::protocol::request::AnthropicHeaders::for_converted(body));
    let call = async {
        if bedrock {
            crate::bedrock::transport::open(&node.upstream.client, dep, credential, body, stream)
                .await
        } else {
            node.upstream
                .open(
                    if dep.is_chat() {
                        crate::upstream::Upstream::resource(&dep.base_url, "chat/completions")
                    } else {
                        crate::upstream::Upstream::endpoint(&dep.base_url, ep, selected.operation)
                    },
                    ep,
                    credential,
                    body,
                    stream,
                    converted_headers.as_ref().unwrap_or(&request.anthropic),
                )
                .await
        }
    };
    let opened = run
        .reservation
        .guarded(async {
            tokio::select! {biased;
                _ = sender.closed(), if !request.background() => None,
                result = call => Some(result),
            }
        })
        .await;
    // Classify unexecuted attempts even when this request gets no further attempt:
    // the caller decides whether another deployment may be tried.
    let response = match opened {
        Ok(Some(Ok(response))) => response,
        Ok(Some(Err(error))) if error.unsent() => {
            if request.interpreter.is_some() {
                let _ = node
                    .db
                    .unused_container(run.reservation.id, selected.key.id, true)
                    .await;
            }
            let reason = match error {
                UpstreamError::Signing => FailoverReason::Signing,
                _ => FailoverReason::Connect,
            };
            return Opened::Declined(None, reason);
        }
        opened => {
            trace.not_answered(&opened);
            return Opened::Lost;
        }
    };
    timer.first_byte();
    if response.status() != StatusCode::BAD_GATEWAY && !crate::upstream::declined(response.status())
    {
        return Opened::Answer(response);
    }
    let reason = FailoverReason::declined(response.status().as_u16());
    // Bedrock's buffered error shape is derived from its HTTP status. Keep the
    // service error header before projecting the response into a client protocol.
    let contract = if bedrock {
        ErrorContract::Bedrock
    } else if ep == Endpoint::AnthropicMessages {
        ErrorContract::Anthropic
    } else {
        ErrorContract::OpenAi
    };
    let aws_error_type = response
        .headers()
        .get("x-amzn-errortype")
        .and_then(|header| header.to_str().ok())
        .map(str::to_owned);
    let rid = run.reservation.id.to_string();
    let limit = node.upstream.response_limit();
    let reply = run
        .reservation
        .guarded(timeout(node.stream_idle, async {
            if dep.is_chat() {
                crate::protocol::chat_upstream::buffered(
                    response,
                    exchange.public_model,
                    request.body(),
                    limit,
                )
                .await
            } else if bedrock && !dep.invoke() {
                crate::bedrock::transport::buffered(
                    response,
                    exchange.public_model,
                    &rid,
                    limit,
                    selected.body.get("guardrailConfig").is_some(),
                )
                .await
            } else {
                node.upstream
                    .buffered(response, ep, selected.operation, selected.bounds.into())
                    .await
            }
        }))
        .await;
    match reply {
        Ok(Ok(Ok(mut reply))) => {
            let unbilled = reply
                .usage
                .raw_usage
                .as_ref()
                .is_none_or(|counts| counts.values().all(|n| *n == 0));
            let refusal = if unbilled {
                refusal(
                    contract,
                    reply.status,
                    aws_error_type.as_deref(),
                    &reply.body,
                )
            } else {
                Refusal::Unknown
            };
            reply.outcome = refusal.outcome();
            if refusal == Refusal::Unknown {
                return Opened::Buffered(reply);
            }
            // Nothing was delivered: the customer is not charged for this attempt.
            if request.interpreter.is_some() {
                let _ = node
                    .db
                    .unused_container(run.reservation.id, selected.key.id, true)
                    .await;
            }
            match refusal {
                Refusal::Declined => Opened::Declined(Some(reply), reason),
                _ => Opened::Buffered(reply),
            }
        }
        _ => Opened::Lost,
    }
}

pub async fn run(
    id: Uuid,
    node: Arc<Node>,
    index: Arc<Index>,
    secret: String,
    mut request: Generate,
    mut sender: Channel,
    mut tally: RequestTally,
) {
    if let Some(parent) = request.previous_response_id().and_then(Generate::handle)
        && let Err(error) =
            crate::control::stored::bind_parent(&node, &index, &secret, &mut request, parent).await
    {
        let _ = sender.send(Err(error));
        return;
    }
    let mut selected = match index.select(&secret, &request) {
        Ok(s) => s,
        Err(e) => {
            let _ = sender.send(Err(e.into()));
            return;
        }
    };
    tally.deployment = Some(selected.deployment.id);
    if sender.is_closed() {
        tally.record(Outcome::ClientCancelled);
        return;
    }
    let (mut reservation, mut credential) = match lifecycle::Reservation::reserve(
        id,
        node.clone(),
        &index,
        &selected,
        selected.upstream.representation(),
        true,
    )
    .await
    {
        Ok(v) => v,
        Err(e) => {
            let _ = sender.send(Err(e));
            return;
        }
    };
    reservation.tally = tally;
    if sender.is_closed() {
        reservation.tally.record(Outcome::ClientCancelled);
        reservation.abort().await;
        return;
    }
    let mut run = match reservation.start().await {
        Ok(v) => v,
        Err(e) => {
            let _ = sender.send(Err(e));
            return;
        }
    };
    let frozen = selected.bounds;
    let deadline = run.retry_deadline(index.policy.failover_budget());
    let mut failed = Vec::new();
    let limit = node.upstream.response_limit();
    let transport = if request.stream {
        Transport::Sse
    } else {
        Transport::Http
    };
    loop {
        let dep = selected.deployment;
        let exchange = Exchange {
            client: request.endpoint,
            upstream: selected.upstream.representation(),
            public_model: &request.model,
        };
        run.approximation = selected.approximation.clone();
        run.history = exchange.converts().then(|| {
            let mut writer = crate::protocol::history::Writer::new(
                node.history.clone(),
                selected.key.id,
                selected.model,
                dep,
                request.endpoint,
            );
            writer.hide_summary = match request.endpoint {
                Endpoint::OpenaiResponses => {
                    request.body().get("reasoning").is_some()
                        && request.body()["reasoning"]["summary"].is_null()
                }
                Endpoint::AnthropicMessages => {
                    request.body()["thinking"]["display"] == "omitted"
                        || request.body()["thinking"]["type"] == "disabled"
                }
            };
            writer
        });
        let trace = Trace {
            request: run.reservation.id,
            deployment: dep.id,
            endpoint: request.endpoint,
            transport,
        };
        if run.approximation.applied.active() {
            let (rules, billing) = run.approximation.applied.labels();
            tracing::info!(request_id=%run.reservation.id,attempt_id=%run.attempt_id(),deployment_id=%dep.id,
                approximation=%rules,billing_basis=%billing,"request approximation applied");
        }
        let mut timer = UpstreamTimer::start(request.endpoint, transport);
        let opened = open(
            &node,
            &mut run,
            &selected,
            exchange,
            &credential,
            &request,
            &mut sender,
            &mut timer,
        )
        .await;
        let answer = match opened {
            Opened::Answer(answer) => answer,
            Opened::Buffered(reply) => {
                deliver(
                    run,
                    sender,
                    reply,
                    exchange,
                    limit,
                    trace,
                    request.chat.is_some(),
                )
                .await;
                return;
            }
            Opened::Lost => {
                let _ = sender.send(Err(Failure::upstream()));
                run.ended(None, false).await;
                return;
            }
            Opened::Declined(reply, reason) => {
                failed.push(dep.id);
                let attempt = run.attempts();
                // Nothing has been written to the client. Another candidate from the same
                // snapshot, within the frozen bounds, gets a newly committed attempt.
                // The gate checks the same conditions in the same order and names the one
                // that stopped failover.
                // Background and Code Interpreter requests keep their single attempt.
                let gate = if run.attempts() >= index.policy.max_attempts()
                    || request.background()
                    || request.interpreter.is_some()
                {
                    Some(FailoverResult::AttemptLimit)
                } else if Instant::now() >= deadline {
                    Some(FailoverResult::BudgetExhausted)
                } else if sender.is_closed() {
                    Some(FailoverResult::ClientGone)
                } else {
                    None
                };
                let next = gate
                    .is_none()
                    .then(|| index.fallback(&secret, &request, &failed, frozen))
                    .flatten();
                let candidate = next.as_ref().map(|n| n.deployment.id);
                let mut result = gate.unwrap_or(FailoverResult::NoCandidate);
                if let Some(next) = next
                    && let Ok(key) = lifecycle::credential(&node, &index, next.deployment).await
                {
                    let target = lifecycle::Target {
                        deployment: next.deployment.id,
                        endpoint: next.upstream.representation(),
                        usage_format: next.usage_format(),
                    };
                    let evidence = reply.as_ref().map_or(
                        UsageEvidence {
                            usage_format: run.usage_format(),
                            raw_usage: None,
                        },
                        |r| r.usage.clone(),
                    );
                    let response_id = reply.as_ref().and_then(|r| r.response_id.as_deref());
                    match run
                        .fail_over(target, evidence, response_id, deadline, &sender)
                        .await
                    {
                        Ok(()) => {
                            if let Err(stop) = run.ready_to_retry(deadline, &sender).await {
                                trace.failover(attempt, candidate, reason, stop);
                                let respond = async {
                                    if let Some(reply) = reply {
                                        let _ = write_reply(
                                            sender,
                                            reply,
                                            exchange,
                                            limit,
                                            request.chat.is_some(),
                                            None,
                                            &selected.approximation,
                                        )
                                        .await;
                                    } else {
                                        let _ = sender.send(Err(Failure::upstream()));
                                    }
                                };
                                // Every earlier attempt was declined and the backup was never
                                // sent, so it is declined as well, whatever stopped it.
                                tokio::join!(
                                    respond,
                                    run.finish_unsent(DECLINED, "upstream_failed")
                                );
                                return;
                            }
                            trace.failover(attempt, candidate, reason, FailoverResult::Switched);
                            selected = next;
                            credential = key;
                            continue;
                        }
                        Err(stop) => result = stop,
                    }
                } else if candidate.is_some() {
                    result = FailoverResult::CredentialUnavailable;
                }
                trace.failover(attempt, candidate, reason, result);
                // The last declined attempt is the client's answer; nothing was executed, so it
                // reports `declined` like the attempts before it.
                match reply {
                    Some(reply) => {
                        deliver(
                            run,
                            sender,
                            reply,
                            exchange,
                            limit,
                            trace,
                            request.chat.is_some(),
                        )
                        .await
                    }
                    None => {
                        let _ = sender.send(Err(Failure::upstream()));
                        let format = run.usage_format();
                        run.finish(
                            DECLINED,
                            "upstream_failed",
                            None,
                            UsageEvidence {
                                usage_format: format,
                                raw_usage: None,
                            },
                        )
                        .await;
                    }
                }
                return;
            }
        };
        let mut resources = (!dep.is_bedrock()).then(|| {
            crate::control::resources::ResourceScope::new(
                &node.db,
                selected.key.id,
                index.revision,
                selected.model,
                dep.id,
            )
            .execution(
                exchange.upstream,
                (selected.bounds.execution_mode != crate::protocol::execution::ExecutionMode::None)
                    .then_some(id),
                request.container_provider(),
            )
            .interpreter(request.interpreter.is_some().then_some(id))
        });
        if request.stream {
            sse::forward(
                node,
                run,
                dep,
                exchange,
                answer,
                sender,
                timer,
                request.chat,
                request.body(),
                request.stored(),
                request.conversation.clone(),
                selected.bounds.into(),
                selected.body.get("guardrailConfig").is_some(),
                request.background(),
                resources,
            )
            .await;
            return;
        }
        let rid = run.reservation.id.to_string();
        let reading = async {
            if dep.is_chat() {
                crate::protocol::chat_upstream::buffered(
                    answer,
                    exchange.public_model,
                    request.body(),
                    limit,
                )
                .await
            } else if dep.is_bedrock() && !dep.invoke() {
                crate::bedrock::transport::buffered(
                    answer,
                    &request.model,
                    &rid,
                    limit,
                    selected.body.get("guardrailConfig").is_some(),
                )
                .await
            } else {
                node.upstream
                    .buffered(
                        answer,
                        exchange.upstream,
                        request.operation,
                        selected.bounds.into(),
                    )
                    .await
            }
        };
        let result = run
            .reservation
            .guarded(async {
                tokio::select! {_=sender.closed(),if !request.background()=>None,r=reading=>Some(r)}
            })
            .await;
        drop(timer);
        if let Ok(Some(Ok(mut reply))) = result {
            if reply.outcome == "pending" && !request.background() {
                let _ = sender.send(Err(Failure::upstream()));
                run.ended(None, false).await;
                return;
            }
            let mut background_register_started = false;
            if (resources.is_some() || request.stored() || request.conversation.is_some())
                && reply.status.is_success()
            {
                let handle = Uuid::new_v4();
                let persisted = async {
                    let mut body: serde_json::Value =
                        serde_json::from_slice(&reply.body).map_err(|_| Failure::upstream())?;
                    if let Some(scope) = &mut resources {
                        let changed = scope
                            .rewrite(&mut body)
                            .await
                            .map_err(|_| Failure::upstream())?;
                        if !changed && !request.stored() && request.conversation.is_none() {
                            return Ok(std::mem::take(&mut reply.body));
                        }
                    }
                    if let Some(conversation) = &request.conversation {
                        conversation
                            .rewrite(&mut body)
                            .map_err(|_| Failure::upstream())?;
                    }
                    if request.stored() {
                        body["id"] = format!("resp_nx_{handle}").into();
                    }
                    let bytes = serde_json::to_vec(&body).map_err(|_| Failure::upstream())?;
                    if bytes.len() > limit {
                        return Err(Failure::upstream());
                    }
                    let provider = reply.response_id.as_deref();
                    if request.background() {
                        let provider = provider.ok_or_else(Failure::upstream)?;
                        background_register_started = true;
                        node.db
                            .register_background(handle, id, run.attempt_id(), provider)
                            .await
                            .map_err(|e| {
                                if !e.retryable() {
                                    background_register_started = false;
                                }
                                Failure::from(e)
                            })?;
                    } else if request.stored() {
                        node.db
                            .save_response(
                                handle,
                                id,
                                run.attempt_id(),
                                provider.ok_or_else(Failure::upstream)?,
                            )
                            .await?;
                    }
                    Ok::<_, Failure>(bytes)
                }
                .await;
                match persisted {
                    Ok(bytes) => reply.body = bytes,
                    Err(error) => {
                        let _ = sender.send(Err(error));
                        if background_register_started {
                            run.background_submitted(None).await;
                        } else if reply.outcome == "pending" {
                            run.ended(None, false).await;
                        } else {
                            run.finish(
                                reply.outcome,
                                "unknown",
                                reply.response_id.as_deref(),
                                reply.usage,
                            )
                            .await;
                        }
                        return;
                    }
                }
            }
            if background_register_started {
                let terminal_usage = (reply.outcome != "pending").then(|| reply.usage.clone());
                let _ = write_reply(
                    sender,
                    reply,
                    exchange,
                    limit,
                    false,
                    None,
                    &run.approximation,
                )
                .await;
                run.background_submitted(terminal_usage.as_ref()).await;
                return;
            }
            deliver(
                run,
                sender,
                reply,
                exchange,
                limit,
                trace,
                request.chat.is_some(),
            )
            .await;
        } else {
            trace.not_answered(&result);
            let _ = sender.send(Err(Failure::upstream()));
            run.ended(None, false).await;
        }
        return;
    }
}

async fn deliver(
    run: lifecycle::Run,
    sender: Channel,
    reply: Reply,
    exchange: Exchange<'_>,
    limit: usize,
    trace: Trace,
    chat: bool,
) {
    let delivered = write_reply(
        sender,
        reply,
        exchange,
        limit,
        chat,
        run.history.as_ref(),
        &run.approximation,
    )
    .await;
    let (reply, written) = match delivered {
        Ok(pair) => pair,
        Err(reply) => {
            trace.rejected("conversion_failed");
            let delivery = if reply.outcome == "unknown" {
                "unknown"
            } else {
                "upstream_failed"
            };
            run.finish(
                reply.outcome,
                delivery,
                reply.response_id.as_deref(),
                reply.usage,
            )
            .await;
            return;
        }
    };
    let delivery = delivery(&reply, written);
    run.finish(
        reply.outcome,
        delivery,
        reply.response_id.as_deref(),
        reply.usage,
    )
    .await;
}

/// The request's delivery outcome once a buffered reply was offered to the client.
fn delivery(reply: &Reply, written: bool) -> &'static str {
    match (reply.outcome, written) {
        ("succeeded", _) if !reply.status.is_success() => "upstream_failed",
        ("succeeded", true) => "succeeded",
        // A declined attempt was not executed; the client got its error and nothing else.
        ("failed" | "declined", _) => "upstream_failed",
        _ => "unknown",
    }
}

async fn write_reply(
    sender: Channel,
    mut reply: Reply,
    exchange: Exchange<'_>,
    limit: usize,
    chat: bool,
    history: Option<&crate::protocol::history::Writer>,
    policy: &crate::protocol::approximation::Delivery,
) -> Result<(Reply, bool), Reply> {
    if exchange.converts() || chat {
        let converted = serde_json::from_slice(&reply.body)
            .ok()
            .and_then(|mut body| {
                let stopped = if let Some(stops) = &policy.stops
                    && reply.status.is_success()
                {
                    stops.clip(&mut body).ok()?
                } else {
                    None
                };
                let (mut value, status) = if exchange.converts() {
                    convert::response::with_history(
                        exchange,
                        &body,
                        &reply.usage,
                        reply.status.as_u16(),
                        history,
                    )
                    .ok()?
                } else {
                    (body, reply.status.as_u16())
                };
                if chat && status < 400 {
                    if value["status"] == "failed"
                        || value.get("error").is_some_and(serde_json::Value::is_object)
                    {
                        return Some((serde_json::json!({"error":value["error"]}), 502));
                    }
                    let mut projected =
                        crate::protocol::chat::response(&value, exchange.public_model).ok()?;
                    if stopped.is_some() {
                        projected["choices"][0]["finish_reason"] = "stop".into();
                    }
                    return Some((projected, status));
                }
                if let Some(marker) = stopped
                    && exchange.client == Endpoint::AnthropicMessages
                {
                    value["stop_reason"] = "stop_sequence".into();
                    value["stop_sequence"] = marker.into();
                }
                Some((value, status))
            });
        let converted = converted
            .map(|(body, status)| (body.to_string().into_bytes(), status))
            .filter(|(wire, _)| wire.len() <= limit);
        let Some((body, status)) = converted else {
            let _ = sender.send(Err(Failure::upstream()));
            return Err(reply);
        };
        reply.body = body;
        reply.status = StatusCode::from_u16(status).expect("HTTP status");
    }
    let (receipt, received) = oneshot::channel();
    let sent = sender
        .send(Ok(Response::Buffered {
            applied: policy.applied,
            status: reply.status,
            body: std::mem::take(&mut reply.body),
            receipt,
        }))
        .is_ok();
    let written = sent && received.await.is_ok();
    Ok((reply, written))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn reply(status: u16, outcome: &'static str) -> Reply {
        Reply {
            status: StatusCode::from_u16(status).unwrap(),
            body: Vec::new(),
            outcome,
            response_id: None,
            usage: UsageEvidence {
                usage_format: "anthropic.messages.v1",
                raw_usage: None,
            },
        }
    }

    #[test]
    fn a_declined_last_attempt_closes_its_request_as_upstream_failed() {
        for written in [true, false] {
            assert_eq!(delivery(&reply(529, DECLINED), written), "upstream_failed");
            assert_eq!(delivery(&reply(429, "failed"), written), "upstream_failed");
            assert_eq!(delivery(&reply(503, "unknown"), written), "unknown");
            assert_eq!(
                delivery(&reply(400, "succeeded"), written),
                "upstream_failed"
            );
        }
        assert_eq!(delivery(&reply(200, "succeeded"), true), "succeeded");
        assert_eq!(delivery(&reply(200, "succeeded"), false), "unknown");
    }
}
