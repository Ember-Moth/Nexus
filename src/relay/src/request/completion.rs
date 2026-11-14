use crate::{
    control::{
        database::Gateway,
        limits::{Lease, Limits},
    },
    protocol::{Endpoint, usage::UsageEvidence},
};
use chrono::Utc;
use serde_json::{Value, json};
use std::time::Duration;
use tokio::time::{Instant, sleep};
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
            eprintln!("unresolved attempt set for request {}", self.request);
        }
        self.submit(db, [Some(finished), self.closable.then_some(closed)])
            .await;
    }
    /// Report an attempt which failed while its request continues with another attempt.
    pub async fn failed(&self, db: &Gateway, response_id: Option<&str>, usage: UsageEvidence) {
        self.submit(db, [Some(self.finished("failed", response_id, usage))])
            .await;
    }
    async fn submit<const N: usize>(&self, db: &Gateway, events: [Option<String>; N]) {
        for event in events.into_iter().flatten() {
            let mut backoff = Duration::from_millis(50);
            let deadline = Instant::now() + Duration::from_secs(60);
            loop {
                match db.append_event(&event).await {
                    Ok(v) if v["status"] == "accepted" => break,
                    Ok(_) => {
                        eprintln!("conflicting usage event for request {}", self.request);
                        break;
                    }
                    Err(_) if Instant::now() < deadline => {
                        sleep(backoff).await;
                        backoff = (backoff * 2).min(Duration::from_secs(2));
                    }
                    Err(_) => {
                        eprintln!("usage submission deadline for request {}", self.request);
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
            if n("cached_input_tokens")? > input
                || n("reasoning_tokens").unwrap_or(0) > output
                || n("cache_write_tokens").unwrap_or(0) > 0
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
    eprintln!("quota finalization unavailable for request {request}");
}
