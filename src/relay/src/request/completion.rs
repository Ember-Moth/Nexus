use crate::{
    control::{
        database::Gateway,
        limits::{Lease, Limits},
    },
    metrics::{self, Abandoned, Outcome, RequestTally},
    protocol::{Endpoint, usage::UsageEvidence},
};
use chrono::Utc;
use serde_json::{Value, json};
use std::time::Duration;
use tokio::time::{Instant, sleep};
use tracing::{error, info, warn};
use uuid::Uuid;

pub struct Attempt {
    pub request: Uuid,
    pub attempt: Uuid,
    pub revision: i64,
    pub usage_format: &'static str,
    pub started: Instant,
    /// Other started attempts of this request. Each reports its own fact; the close lists all.
    pub others: Vec<Uuid>,
    /// False when an attempt start had an unknown result, leaving the started set for review.
    pub closable: bool,
    pub tally: RequestTally,
}

impl Attempt {
    fn event(&self, kind: &str, aid: Option<Uuid>, payload: Value) -> String {
        json!({
            "schema_version": 1,
            "event_id": Uuid::new_v4(),
            "event_type": kind,
            "occurred_at": Utc::now().to_rfc3339(),
            "request_id": self.request,
            "attempt_id": aid,
            "config_revision": self.revision.to_string(),
            "payload": payload,
        })
        .to_string()
    }
    fn finished(&self, outcome: &str, response_id: Option<&str>, usage: UsageEvidence) -> String {
        let response_id = response_id.filter(|s| {
            s.len() <= 200
                && s.bytes()
                    .all(|b| b.is_ascii_alphanumeric() || b"_.:/-".contains(&b))
        });
        self.event(
            "attempt.finished",
            Some(self.attempt),
            json!({
                "outcome": outcome,
                "elapsed_ms": self.started.elapsed().as_millis().min(u32::MAX as u128) as u32,
                "upstream_request_id": response_id,
                "usage_format": usage.usage_format,
                "raw_usage": usage.raw_usage,
            }),
        )
    }
    pub async fn complete(
        &self,
        db: &Gateway,
        outcome: &str,
        delivery: &str,
        attempt_delivered: bool,
        response_id: Option<&str>,
        usage: UsageEvidence,
    ) {
        let attempts = self.others.len() + 1;
        self.tally.record(Outcome::from_delivery(delivery));
        self.tally.observe_attempts(attempts);
        info!(
            request_id = %self.request,
            attempt_id = %self.attempt,
            deployment_id = self.tally.deployment.map(tracing::field::display),
            endpoint = self.tally.endpoint(),
            transport = self.tally.transport(),
            outcome,
            delivery,
            attempts = attempts as u64,
            elapsed_ms = self.started.elapsed().as_millis() as u64,
            "request finished"
        );
        let finished = self.finished(outcome, response_id, usage);
        let mut started = self.others.clone();
        started.push(self.attempt);
        let closed = self.event(
            "request.closed",
            None,
            json!({
                "delivery_outcome": delivery,
                "attempt_ids": started,
                "delivered_attempt_id": attempt_delivered.then_some(self.attempt),
            }),
        );
        if !self.closable {
            warn!(
                request_id = %self.request,
                attempt_id = %self.attempt,
                "attempt set unresolved; request close withheld for review"
            );
        }
        self.submit(
            db,
            [
                Some(("attempt.finished", finished)),
                self.closable.then_some(("request.closed", closed)),
            ],
        )
        .await;
    }
    /// Report an attempt which failed while its request continues with another attempt.
    pub async fn failed(&self, db: &Gateway, response_id: Option<&str>, usage: UsageEvidence) {
        let finished = self.finished("failed", response_id, usage);
        self.submit(db, [Some(("attempt.finished", finished))])
            .await;
    }
    async fn submit<const N: usize>(
        &self,
        db: &Gateway,
        events: [Option<(&'static str, String)>; N],
    ) {
        let _pending = metrics::completion_pending();
        for (event_type, event) in events.into_iter().flatten() {
            let mut backoff = Duration::from_millis(50);
            let deadline = Instant::now() + Duration::from_secs(60);
            let mut replays = 0u32;
            loop {
                match db.append_event(&event).await {
                    Ok(v) if v["status"] == "accepted" => {
                        if replays > 0 {
                            info!(
                                request_id = %self.request,
                                attempt_id = %self.attempt,
                                event_type,
                                replays,
                                "completion event accepted after replay"
                            );
                        }
                        break;
                    }
                    Ok(_) => {
                        metrics::completion_abandoned(Abandoned::Conflict);
                        error!(
                            request_id = %self.request,
                            attempt_id = %self.attempt,
                            event_type,
                            "completion event conflicts with recorded facts"
                        );
                        break;
                    }
                    Err(e) if Instant::now() < deadline => {
                        replays += 1;
                        metrics::completion_replay();
                        if replays == 1 {
                            warn!(
                                request_id = %self.request,
                                attempt_id = %self.attempt,
                                event_type,
                                reason = e.reason(),
                                "completion event replay scheduled"
                            );
                        }
                        sleep(backoff).await;
                        backoff = (backoff * 2).min(Duration::from_secs(2));
                    }
                    Err(e) => {
                        metrics::completion_abandoned(Abandoned::Deadline);
                        error!(
                            request_id = %self.request,
                            attempt_id = %self.attempt,
                            event_type,
                            replays,
                            reason = e.reason(),
                            "completion event abandoned after replay deadline"
                        );
                        break;
                    }
                }
            }
        }
    }
    pub async fn unknown(&self, db: &Gateway) {
        self.complete(
            db,
            "unknown",
            "unknown",
            false,
            None,
            UsageEvidence {
                usage_format: self.usage_format,
                raw_usage: None,
            },
        )
        .await
    }
}

// TPM counts physical tokens, including both cache classes exactly once.
pub fn confirmed_tokens(endpoint: Endpoint, usage: &UsageEvidence) -> Option<u64> {
    if usage.usage_format == crate::bedrock::FORMAT {
        return crate::bedrock::wire::total(usage);
    }
    let raw = usage.raw_usage.as_ref()?;
    let n = |k: &str| raw.get(k).copied().map(u64::from);
    let input = n("input_tokens")?;
    let output = n("output_tokens")?;
    match endpoint {
        Endpoint::OpenaiResponses => {
            let writes = n("cache_write_tokens").unwrap_or(0);
            // The Chat totals include cache hits even when the optional split is absent.
            let cached = if usage.usage_format == crate::protocol::chat_upstream::FORMAT {
                n("cached_input_tokens").unwrap_or(0)
            } else {
                n("cached_input_tokens")?
            };
            if cached + writes > input
                || n("reasoning_tokens").unwrap_or(0) > output
                || (writes > 0
                    && (n("cache_write_30m_tokens")? + n("cache_write_12h_tokens")? != writes))
                || n("total_tokens").is_some_and(|total| total != input + output)
            {
                return None;
            }
            Some(input + output)
        }
        Endpoint::AnthropicMessages => {
            let created = n("cache_creation_input_tokens")?;
            if created != 0
                && (n("cache_write_5m_tokens").is_none() || n("cache_write_1h_tokens").is_none())
                || created
                    != n("cache_write_5m_tokens").unwrap_or(0)
                        + n("cache_write_1h_tokens").unwrap_or(0)
            {
                return None;
            }
            Some(input + output + n("cache_read_input_tokens")? + created)
        }
    }
}

pub async fn finish_lease(limits: &Limits, lease: &Lease, actual: Option<u64>, request: Uuid) {
    for _ in 0..3 {
        match limits.finish(lease, actual).await {
            Ok(()) => return,
            Err(crate::control::limits::LimitError::Expired) => return,
            Err(_) => sleep(Duration::from_millis(100)).await,
        }
    }
    warn!(request_id = %request, "quota lease finalization unavailable");
}
