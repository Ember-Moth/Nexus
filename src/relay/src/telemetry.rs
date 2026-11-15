//! Structured relay logs: one JSON object per line on stderr.
//!
//! Only fields named in [`LOG_FIELDS`] keep their values. Every other field, including
//! fields emitted by dependencies such as request signers and database drivers, is
//! written as [`REDACTED`]. Allowlisted fields carry identifiers, enumerations, counts
//! and durations only: never an enterprise Key, upstream credential, the cluster
//! private key, a request or response body, or tool arguments. Messages are static
//! text; `tests/logging.rs` rejects interpolated messages and unlisted field names.
use chrono::{SecondsFormat, Utc};
use serde_json::Value;
use std::{error::Error, fmt, io::Write};
use tracing::{
    Event, Subscriber,
    field::{Field, Visit},
};
use tracing_subscriber::{
    Layer, filter::Targets, layer::Context, layer::SubscriberExt, util::SubscriberInitExt,
};

/// Field names whose values may appear in logs.
pub const LOG_FIELDS: &[&str] = &[
    "message",
    "request_id",
    "attempt_id",
    "attempt",
    "attempts",
    "deployment_id",
    "approximation",
    "billing_basis",
    "next_deployment_id",
    "cluster_id",
    "node_id",
    "session_id",
    "endpoint",
    "transport",
    "outcome",
    "delivery",
    "event_type",
    "reason",
    "error",
    "revision",
    "elapsed_ms",
    "replays",
    "turns",
    "reused",
    "reusable",
    "listen",
    "in_flight",
    "version",
    "purge_id",
    "resource_kind",
    "provider_id",
];

/// Replacement value for fields outside [`LOG_FIELDS`].
pub const REDACTED: &str = "[redacted]";

/// Default filter when `NEXUS_RELAY_LOG` is unset.
pub const DEFAULT_FILTER: &str = "info";

/// Install the process-wide JSON logger. `directives` uses `target=level` pairs
/// separated by commas; a bare level sets the default, e.g. `info,sqlx=warn`.
pub fn init(directives: Option<&str>) -> Result<(), Box<dyn Error>> {
    let filter: Targets = directives
        .unwrap_or(DEFAULT_FILTER)
        .parse()
        .map_err(|_| "invalid NEXUS_RELAY_LOG")?;
    tracing_subscriber::registry()
        .with(JsonLayer::new(std::io::stderr).with_filter(filter))
        .try_init()?;
    Ok(())
}

/// Formats events as JSON lines and redacts values of unlisted fields.
pub struct JsonLayer<W> {
    writer: W,
}

impl<W> JsonLayer<W> {
    pub fn new(writer: W) -> Self {
        Self { writer }
    }
}

impl<S, W, T> Layer<S> for JsonLayer<W>
where
    S: Subscriber,
    W: Fn() -> T + Send + Sync + 'static,
    T: Write,
{
    fn on_event(&self, event: &Event<'_>, _: Context<'_, S>) {
        let meta = event.metadata();
        let mut record = Record(vec![
            (
                "timestamp",
                Utc::now()
                    .to_rfc3339_opts(SecondsFormat::Millis, true)
                    .into(),
            ),
            ("level", meta.level().as_str().into()),
            ("target", meta.target().into()),
        ]);
        event.record(&mut record);
        let _ = (self.writer)().write_all(record.line().as_bytes());
    }
}

struct Record(Vec<(&'static str, Value)>);

impl Record {
    fn push(&mut self, field: &Field, value: impl FnOnce() -> Value) {
        let name = field.name();
        let value = if LOG_FIELDS.contains(&name) {
            value()
        } else {
            REDACTED.into()
        };
        self.0.push((name, value));
    }
    fn line(&self) -> String {
        let mut line = String::from("{");
        for (i, (name, value)) in self.0.iter().enumerate() {
            if i > 0 {
                line.push(',');
            }
            line.push_str(&Value::from(*name).to_string());
            line.push(':');
            line.push_str(&value.to_string());
        }
        line.push_str("}\n");
        line
    }
}

impl Visit for Record {
    fn record_debug(&mut self, field: &Field, value: &dyn fmt::Debug) {
        self.push(field, || format!("{value:?}").into());
    }
    fn record_str(&mut self, field: &Field, value: &str) {
        self.push(field, || value.into());
    }
    fn record_i64(&mut self, field: &Field, value: i64) {
        self.push(field, || value.into());
    }
    fn record_u64(&mut self, field: &Field, value: u64) {
        self.push(field, || value.into());
    }
    fn record_bool(&mut self, field: &Field, value: bool) {
        self.push(field, || value.into());
    }
    fn record_f64(&mut self, field: &Field, value: f64) {
        self.push(field, || value.into());
    }
    fn record_error(&mut self, field: &Field, value: &(dyn Error + 'static)) {
        self.push(field, || value.to_string().into());
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::{Arc, Mutex};

    #[derive(Clone, Default)]
    struct Buffer(Arc<Mutex<Vec<u8>>>);
    impl Write for Buffer {
        fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
            self.0.lock().unwrap().write(bytes)
        }
        fn flush(&mut self) -> std::io::Result<()> {
            Ok(())
        }
    }

    fn capture(filter: &str, emit: impl FnOnce()) -> Vec<serde_json::Map<String, Value>> {
        let buffer = Buffer::default();
        let sink = buffer.clone();
        let subscriber = tracing_subscriber::registry().with(
            JsonLayer::new(move || sink.clone()).with_filter(filter.parse::<Targets>().unwrap()),
        );
        tracing::subscriber::with_default(subscriber, emit);
        let text = String::from_utf8(buffer.0.lock().unwrap().clone()).unwrap();
        text.lines()
            .map(|line| match serde_json::from_str(line).unwrap() {
                Value::Object(map) => map,
                other => panic!("log line is not an object: {other}"),
            })
            .collect()
    }

    #[test]
    fn secrets_in_unlisted_fields_never_reach_the_output() {
        let key = "sk-ant-enterprise-call-key-0123456789";
        let upstream = "upstream-provider-secret-abcdef";
        let private = "Y2x1c3Rlci1wcml2YXRlLWtleQ==";
        let body = r#"{"messages":[{"role":"user","content":"confidential prompt"}]}"#;
        let arguments = r#"{"account":"tool-argument-value"}"#;
        let request = uuid::Uuid::new_v4();
        let lines = capture("trace", || {
            tracing::warn!(
                request_id = %request,
                endpoint = "openai_responses",
                elapsed_ms = 12u64,
                api_key = key,
                authorization = %format!("Bearer {upstream}"),
                private_key = private,
                body = body,
                arguments = ?arguments,
                "upstream request failed"
            );
            // Dependencies emit their own fields, e.g. a signer's canonical request.
            tracing::debug!(target: "aws_sigv4::sign", canonical_request = %upstream, "calculated signing parameters");
        });
        let text = format!("{lines:?}");
        for secret in [
            key,
            upstream,
            private,
            "confidential prompt",
            "tool-argument-value",
        ] {
            assert!(!text.contains(secret), "{secret} leaked into {text}");
        }
        let first = &lines[0];
        assert_eq!(first["message"], "upstream request failed");
        assert_eq!(first["level"], "WARN");
        assert_eq!(first["request_id"], request.to_string());
        assert_eq!(first["endpoint"], "openai_responses");
        assert_eq!(first["elapsed_ms"], 12);
        for name in [
            "api_key",
            "authorization",
            "private_key",
            "body",
            "arguments",
        ] {
            assert_eq!(first[name], REDACTED, "{name}");
        }
        assert_eq!(lines[1]["target"], "aws_sigv4::sign");
        assert_eq!(lines[1]["canonical_request"], REDACTED);
    }

    #[test]
    fn filter_directives_select_levels_per_target() {
        let lines = capture("warn,nexus_relay=debug", || {
            tracing::debug!(target: "nexus_relay::server", "kept");
            tracing::info!(target: "sqlx::query", "dropped");
            tracing::warn!(target: "sqlx::query", "kept");
        });
        assert_eq!(lines.len(), 2);
        assert!(lines.iter().all(|line| line["message"] == "kept"));
        assert!("nexus_relay=loud".parse::<Targets>().is_err());
        assert!(DEFAULT_FILTER.parse::<Targets>().is_ok());
    }
}
