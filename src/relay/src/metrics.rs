//! Prometheus metrics in text exposition format 0.0.4, served on a listener that is
//! separate from enterprise traffic (`NEXUS_RELAY_METRICS_LISTEN`).
//!
//! Every label value is a closed enum or a `&'static str` operation name written at
//! the call site, so the set of series is fixed when the binary is built. Request,
//! organization, project, deployment and Key identifiers are never labels.
use crate::protocol::Endpoint;
use axum::{
    Router,
    http::{StatusCode, header},
    response::{IntoResponse, Response},
    routing::get,
};
use prometheus_client::{
    encoding::{EncodeLabelSet, EncodeLabelValue, LabelValueEncoder, prometheus_text},
    metrics::{counter::Counter, family::Family, gauge::Gauge, histogram::Histogram},
    registry::Registry,
};
use std::{
    fmt::{self, Write as _},
    sync::{
        LazyLock,
        atomic::{AtomicBool, Ordering},
    },
    time::Instant,
};
use tokio::net::TcpListener;
use uuid::Uuid;

macro_rules! label {
    ($(#[$doc:meta])* $ty:ident { $($variant:ident => $name:literal),+ $(,)? }) => {
        $(#[$doc])*
        #[derive(Clone, Copy, Debug, Hash, PartialEq, Eq)]
        pub enum $ty { $($variant),+ }
        impl $ty {
            pub const ALL: &[Self] = &[$(Self::$variant),+];
            pub fn as_str(self) -> &'static str {
                match self { $(Self::$variant => $name),+ }
            }
        }
        impl EncodeLabelValue for $ty {
            fn encode(&self, encoder: &mut LabelValueEncoder) -> fmt::Result {
                encoder.write_str(self.as_str())
            }
        }
    };
}

label!(
    /// How the client exchanged this request with the relay.
    Transport { Http => "http", Sse => "sse", Websocket => "websocket" }
);
label!(
    /// Final result of one generation request or WebSocket turn. The first four are the
    /// delivery outcomes reported in `request.closed`; `Rejected` means it ended before
    /// any upstream attempt started.
    Outcome {
        Succeeded => "succeeded",
        UpstreamFailed => "upstream_failed",
        ClientCancelled => "client_cancelled",
        Unknown => "unknown",
        Rejected => "rejected",
    }
);
impl Outcome {
    /// Map a `request.closed` delivery outcome; unrecognized values are `Unknown`.
    pub fn from_delivery(delivery: &str) -> Self {
        match delivery {
            "succeeded" => Self::Succeeded,
            "upstream_failed" => Self::UpstreamFailed,
            "client_cancelled" => Self::ClientCancelled,
            _ => Self::Unknown,
        }
    }
}
label!(
    /// Why an HTTP or SSE attempt provably did not execute: the request never left the node
    /// (`connect`, `signing`) or the upstream declined it with a status before executing it.
    FailoverReason {
        Connect => "connect",
        Signing => "signing",
        Status429 => "status_429",
        Status503 => "status_503",
        Status529 => "status_529",
        OtherStatus => "status_other",
    }
);
impl FailoverReason {
    pub fn declined(status: u16) -> Self {
        match status {
            429 => Self::Status429,
            503 => Self::Status503,
            529 => Self::Status529,
            _ => Self::OtherStatus,
        }
    }
}
label!(
    /// What followed an attempt that provably did not execute. Only `Switched` sends
    /// another attempt; the others answer the client with the last declined result.
    FailoverResult {
        Switched => "switched",
        NoCandidate => "no_candidate",
        CredentialUnavailable => "credential_unavailable",
        StartFailed => "start_failed",
        AttemptLimit => "attempt_limit",
        BudgetExhausted => "budget_exhausted",
        ClientGone => "client_gone",
    }
);
label!(
    /// A control-plane dependency of the node.
    Dependency { Database => "database", Redis => "redis" }
);
label!(
    /// `Rejected`: the gateway contract answered with a business error (SQLSTATE P0001).
    /// `Unavailable`: transport, timeout, pool, or protocol failure.
    ErrorKind { Unavailable => "unavailable", Rejected => "rejected" }
);
label!(
    /// Why a completion event stopped being replayed before it was accepted.
    Abandoned { Conflict => "conflict", Deadline => "deadline" }
);

impl EncodeLabelValue for Endpoint {
    fn encode(&self, encoder: &mut LabelValueEncoder) -> fmt::Result {
        encoder.write_str(self.as_str())
    }
}

/// Client endpoint and transport pairs that the relay serves.
const EXCHANGES: [(Endpoint, Transport); 5] = [
    (Endpoint::AnthropicMessages, Transport::Http),
    (Endpoint::AnthropicMessages, Transport::Sse),
    (Endpoint::OpenaiResponses, Transport::Http),
    (Endpoint::OpenaiResponses, Transport::Sse),
    (Endpoint::OpenaiResponses, Transport::Websocket),
];

#[derive(Clone, Debug, Hash, PartialEq, Eq, EncodeLabelSet)]
struct RequestLabels {
    endpoint: Endpoint,
    transport: Transport,
    outcome: Outcome,
}

#[derive(Clone, Debug, Hash, PartialEq, Eq, EncodeLabelSet)]
struct ExchangeLabels {
    endpoint: Endpoint,
    transport: Transport,
}

#[derive(Clone, Debug, Hash, PartialEq, Eq, EncodeLabelSet)]
struct ControlLabels {
    dependency: Dependency,
    operation: &'static str,
    kind: ErrorKind,
}

#[derive(Clone, Debug, Hash, PartialEq, Eq, EncodeLabelSet)]
struct AbandonLabels {
    reason: Abandoned,
}

#[derive(Clone, Debug, Hash, PartialEq, Eq, EncodeLabelSet)]
struct FailoverLabels {
    reason: FailoverReason,
    result: FailoverResult,
}

type Histograms = Family<ExchangeLabels, Histogram, fn() -> Histogram>;

fn first_byte_histogram() -> Histogram {
    Histogram::new([0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0])
}

fn attempts_histogram() -> Histogram {
    // NEXUS_RELAY_MAX_ATTEMPTS is at most 4.
    Histogram::new([1.0, 2.0, 3.0, 4.0])
}

fn duration_histogram() -> Histogram {
    Histogram::new([
        0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0, 600.0, 1800.0, 3600.0,
    ])
}

/// Node state read when Prometheus scrapes.
#[derive(Clone, Copy, Debug, Default)]
pub struct NodeSample {
    pub healthy: bool,
    pub revision: Option<i64>,
    pub capacity_limit: usize,
    pub capacity_available: usize,
    pub websocket_sessions: usize,
    pub websocket_turns: usize,
}

pub struct Metrics {
    registry: Registry,
    requests: Family<RequestLabels, Counter>,
    first_byte: Histograms,
    duration: Histograms,
    sse_streams: Gauge,
    websocket_sessions: Gauge,
    websocket_turns: Gauge,
    control_errors: Family<ControlLabels, Counter>,
    snapshot_revision: Gauge,
    control_plane_healthy: Gauge,
    draining: Gauge,
    completions_pending: Gauge,
    completion_replays: Counter,
    completions_abandoned: Family<AbandonLabels, Counter>,
    capacity_in_use: Gauge,
    capacity_limit: Gauge,
    failovers: Family<FailoverLabels, Counter>,
    attempts: Histograms,
}

impl Default for Metrics {
    fn default() -> Self {
        Self::new()
    }
}

impl Metrics {
    pub fn new() -> Self {
        let mut registry = Registry::with_prefix("nexus_relay");
        let metrics = Self {
            requests: Family::default(),
            first_byte: Family::new_with_constructor(first_byte_histogram),
            duration: Family::new_with_constructor(duration_histogram),
            sse_streams: Gauge::default(),
            websocket_sessions: Gauge::default(),
            websocket_turns: Gauge::default(),
            control_errors: Family::default(),
            snapshot_revision: Gauge::default(),
            control_plane_healthy: Gauge::default(),
            draining: Gauge::default(),
            completions_pending: Gauge::default(),
            completion_replays: Counter::default(),
            completions_abandoned: Family::default(),
            capacity_in_use: Gauge::default(),
            capacity_limit: Gauge::default(),
            failovers: Family::default(),
            attempts: Family::new_with_constructor(attempts_histogram),
            registry: Registry::default(),
        };
        // Create every bounded series up front so rates start from zero.
        for (endpoint, transport) in EXCHANGES {
            for &outcome in Outcome::ALL {
                let labels = RequestLabels {
                    endpoint,
                    transport,
                    outcome,
                };
                drop(metrics.requests.get_or_create(&labels));
            }
            let labels = ExchangeLabels {
                endpoint,
                transport,
            };
            drop(metrics.first_byte.get_or_create(&labels));
            drop(metrics.duration.get_or_create(&labels));
            drop(metrics.attempts.get_or_create(&labels));
        }
        for &reason in Abandoned::ALL {
            drop(
                metrics
                    .completions_abandoned
                    .get_or_create(&AbandonLabels { reason }),
            );
        }
        registry.register(
            "requests",
            "Generation requests and WebSocket turns by client endpoint, transport and final outcome",
            metrics.requests.clone(),
        );
        registry.register(
            "upstream_first_byte_seconds",
            "Time from sending an upstream request to its response headers, or to the first frame of a WebSocket turn",
            metrics.first_byte.clone(),
        );
        registry.register(
            "upstream_duration_seconds",
            "Time from sending an upstream request until it completed, failed, or was abandoned",
            metrics.duration.clone(),
        );
        registry.register(
            "sse_streams_active",
            "SSE responses currently streaming to clients",
            metrics.sse_streams.clone(),
        );
        registry.register(
            "websocket_sessions_active",
            "Client WebSocket sessions holding a node session slot",
            metrics.websocket_sessions.clone(),
        );
        registry.register(
            "websocket_turns_active",
            "WebSocket turns currently holding a tenant turn slot",
            metrics.websocket_turns.clone(),
        );
        registry.register(
            "control_errors",
            "Failed gateway_v1 database calls and Redis commands by operation",
            metrics.control_errors.clone(),
        );
        registry.register(
            "snapshot_revision",
            "Configuration snapshot revision installed on this node, 0 before the first one",
            metrics.snapshot_revision.clone(),
        );
        registry.register(
            "control_plane_healthy",
            "1 while the latest snapshot refresh and heartbeat succeeded",
            metrics.control_plane_healthy.clone(),
        );
        registry.register(
            "draining",
            "1 after shutdown started and the node stopped accepting connections",
            metrics.draining.clone(),
        );
        registry.register(
            "completion_events_pending",
            "Requests whose completion events are not yet accepted by the gateway",
            metrics.completions_pending.clone(),
        );
        registry.register(
            "completion_event_replays",
            "Completion event submissions retried after a failed or unknown commit",
            metrics.completion_replays.clone(),
        );
        registry.register(
            "completion_events_abandoned",
            "Completion events given up after a conflict or the replay deadline",
            metrics.completions_abandoned.clone(),
        );
        registry.register(
            "failover_decisions",
            "HTTP and SSE attempts that provably did not execute, by cause and by what followed; result=switched counts retries on another deployment",
            metrics.failovers.clone(),
        );
        registry.register(
            "request_attempts",
            "Upstream attempts per request, observed when a request that started at least one closes",
            metrics.attempts.clone(),
        );
        registry.register(
            "capacity_in_use",
            "Node concurrency permits held by requests, turns and pending reports",
            metrics.capacity_in_use.clone(),
        );
        registry.register(
            "capacity_limit",
            "Configured node concurrency permits (NEXUS_RELAY_CONCURRENCY)",
            metrics.capacity_limit.clone(),
        );
        Self {
            registry,
            ..metrics
        }
    }

    fn request(&self, endpoint: Endpoint, transport: Transport, outcome: Outcome) {
        let labels = RequestLabels {
            endpoint,
            transport,
            outcome,
        };
        self.requests.get_or_create(&labels).inc();
    }

    /// Encode all metrics after reading the scrape-time node state.
    pub fn render(&self, sample: &NodeSample) -> String {
        let gauge = |n: usize| i64::try_from(n).unwrap_or(i64::MAX);
        self.control_plane_healthy.set(sample.healthy.into());
        self.snapshot_revision.set(sample.revision.unwrap_or(0));
        self.capacity_limit.set(gauge(sample.capacity_limit));
        self.capacity_in_use.set(gauge(
            sample
                .capacity_limit
                .saturating_sub(sample.capacity_available),
        ));
        self.websocket_sessions
            .set(gauge(sample.websocket_sessions));
        self.websocket_turns.set(gauge(sample.websocket_turns));
        let mut text = String::new();
        prometheus_text::encode(&mut text, &self.registry).expect("encoding into a String");
        text
    }
}

static GLOBAL: LazyLock<Metrics> = LazyLock::new(Metrics::new);

/// The process registry served by [`serve`].
pub fn global() -> &'static Metrics {
    &GLOBAL
}

pub fn control_error(dependency: Dependency, operation: &'static str, kind: ErrorKind) {
    let labels = ControlLabels {
        dependency,
        operation,
        kind,
    };
    global().control_errors.get_or_create(&labels).inc();
}

pub fn failover(reason: FailoverReason, result: FailoverResult) {
    global()
        .failovers
        .get_or_create(&FailoverLabels { reason, result })
        .inc();
}

pub fn completion_replay() {
    global().completion_replays.inc();
}

pub fn completion_abandoned(reason: Abandoned) {
    global()
        .completions_abandoned
        .get_or_create(&AbandonLabels { reason })
        .inc();
}

pub fn set_draining(draining: bool) {
    global().draining.set(draining.into());
}

/// Holds one unit of a gauge until dropped.
#[must_use]
pub struct Active(&'static Gauge);
impl Active {
    fn enter(gauge: &'static Gauge) -> Self {
        gauge.inc();
        Self(gauge)
    }
}
impl Drop for Active {
    fn drop(&mut self) {
        self.0.dec();
    }
}

pub fn sse_stream() -> Active {
    Active::enter(&global().sse_streams)
}

pub fn completion_pending() -> Active {
    Active::enter(&global().completions_pending)
}

/// Counts one generation request or WebSocket turn exactly once. A tally dropped
/// without an outcome counts as [`Outcome::Rejected`]. It also carries the log
/// context that is known before the attempt starts.
pub struct RequestTally {
    labels: Option<(Endpoint, Transport)>,
    pub deployment: Option<Uuid>,
    recorded: AtomicBool,
}

impl Default for RequestTally {
    fn default() -> Self {
        Self::detached()
    }
}

impl RequestTally {
    pub fn new(endpoint: Endpoint, transport: Transport) -> Self {
        Self {
            labels: Some((endpoint, transport)),
            deployment: None,
            recorded: AtomicBool::new(false),
        }
    }
    /// A placeholder that records nothing.
    pub fn detached() -> Self {
        Self {
            labels: None,
            deployment: None,
            recorded: AtomicBool::new(true),
        }
    }
    pub fn set_transport(&mut self, transport: Transport) {
        if let Some((_, current)) = &mut self.labels {
            *current = transport;
        }
    }
    pub fn endpoint(&self) -> Option<&'static str> {
        self.labels.map(|(endpoint, _)| endpoint.as_str())
    }
    pub fn transport(&self) -> Option<&'static str> {
        self.labels.map(|(_, transport)| transport.as_str())
    }
    /// Observe how many attempts the request started, once, when it closes.
    pub fn observe_attempts(&self, attempts: usize) {
        if let Some((endpoint, transport)) = self.labels {
            let labels = ExchangeLabels {
                endpoint,
                transport,
            };
            global()
                .attempts
                .get_or_create(&labels)
                .observe(attempts as f64);
        }
    }
    /// Record the outcome. Only the first call counts.
    pub fn record(&self, outcome: Outcome) {
        if let Some((endpoint, transport)) = self.labels
            && !self.recorded.swap(true, Ordering::AcqRel)
        {
            global().request(endpoint, transport, outcome);
        }
    }
}

impl Drop for RequestTally {
    fn drop(&mut self) {
        self.record(Outcome::Rejected);
    }
}

/// Observes upstream latency: the first byte when marked, the total when dropped.
pub struct UpstreamTimer {
    labels: ExchangeLabels,
    started: Instant,
    first_byte: bool,
}

impl UpstreamTimer {
    pub fn start(endpoint: Endpoint, transport: Transport) -> Self {
        Self {
            labels: ExchangeLabels {
                endpoint,
                transport,
            },
            started: Instant::now(),
            first_byte: false,
        }
    }
    pub fn first_byte(&mut self) {
        if !self.first_byte {
            self.first_byte = true;
            global()
                .first_byte
                .get_or_create(&self.labels)
                .observe(self.started.elapsed().as_secs_f64());
        }
    }
}

impl Drop for UpstreamTimer {
    fn drop(&mut self) {
        global()
            .duration
            .get_or_create(&self.labels)
            .observe(self.started.elapsed().as_secs_f64());
    }
}

/// Liveness: the process and its event loop still answer requests. It checks no
/// dependency; `/ready` on the main listener covers PostgreSQL, Redis and capacity.
pub async fn live() -> StatusCode {
    StatusCode::OK
}

/// Routes of the metrics listener: `GET /metrics` and `GET /live`.
pub fn router<F>(sample: F) -> Router
where
    F: Fn() -> NodeSample + Clone + Send + Sync + 'static,
{
    Router::new()
        .route(
            "/metrics",
            get(move || std::future::ready(scrape(&sample()))),
        )
        .route("/live", get(live))
}

fn scrape(sample: &NodeSample) -> Response {
    (
        [
            (header::CONTENT_TYPE, prometheus_text::CONTENT_TYPE),
            (header::CACHE_CONTROL, "no-store"),
        ],
        global().render(sample),
    )
        .into_response()
}

/// Serve the metrics listener until the process exits, including while draining.
pub async fn serve<F>(listener: TcpListener, sample: F) -> std::io::Result<()>
where
    F: Fn() -> NodeSample + Clone + Send + Sync + 'static,
{
    axum::serve(listener, router(sample)).await
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::BTreeSet;

    fn sample() -> NodeSample {
        NodeSample {
            healthy: true,
            revision: Some(42),
            capacity_limit: 8,
            capacity_available: 5,
            websocket_sessions: 2,
            websocket_turns: 1,
        }
    }

    /// Label values of every sample line in the exposition.
    fn label_values(text: &str) -> BTreeSet<String> {
        let mut values = BTreeSet::new();
        for line in text.lines().filter(|l| !l.starts_with('#')) {
            let Some((_, rest)) = line.split_once('{') else {
                continue;
            };
            let labels = rest.rsplit_once('}').unwrap().0;
            for pair in labels.split(',') {
                let (name, value) = pair.split_once('=').unwrap();
                if name != "le" {
                    values.insert(value.trim_matches('"').to_owned());
                }
            }
        }
        values
    }

    #[test]
    fn renders_prometheus_text_with_bounded_series() {
        let metrics = Metrics::new();
        let before = metrics.render(&sample());
        metrics.request(
            Endpoint::OpenaiResponses,
            Transport::Websocket,
            Outcome::Succeeded,
        );
        metrics
            .first_byte
            .get_or_create(&ExchangeLabels {
                endpoint: Endpoint::AnthropicMessages,
                transport: Transport::Sse,
            })
            .observe(0.2);
        let labels = ControlLabels {
            dependency: Dependency::Database,
            operation: "reserve",
            kind: ErrorKind::Unavailable,
        };
        metrics.control_errors.get_or_create(&labels).inc();
        metrics.completion_replays.inc();
        let failover = FailoverLabels {
            reason: FailoverReason::declined(503),
            result: FailoverResult::Switched,
        };
        metrics.failovers.get_or_create(&failover).inc();
        let labels = ExchangeLabels {
            endpoint: Endpoint::AnthropicMessages,
            transport: Transport::Http,
        };
        metrics.attempts.get_or_create(&labels).observe(2.0);
        let text = metrics.render(&sample());
        for expected in [
            "# TYPE nexus_relay_requests_total counter",
            r#"nexus_relay_requests_total{endpoint="openai_responses",transport="websocket",outcome="succeeded"} 1"#,
            r#"nexus_relay_requests_total{endpoint="anthropic_messages",transport="http",outcome="rejected"} 0"#,
            "# TYPE nexus_relay_upstream_first_byte_seconds histogram",
            r#"nexus_relay_upstream_first_byte_seconds_bucket{endpoint="anthropic_messages",transport="sse",le="0.25"} 1"#,
            r#"nexus_relay_upstream_first_byte_seconds_count{endpoint="anthropic_messages",transport="sse"} 1"#,
            "# TYPE nexus_relay_upstream_duration_seconds histogram",
            r#"nexus_relay_control_errors_total{dependency="database",operation="reserve",kind="unavailable"} 1"#,
            "nexus_relay_snapshot_revision 42",
            "nexus_relay_control_plane_healthy 1",
            "nexus_relay_draining 0",
            "nexus_relay_capacity_limit 8",
            "nexus_relay_capacity_in_use 3",
            "nexus_relay_websocket_sessions_active 2",
            "nexus_relay_websocket_turns_active 1",
            "nexus_relay_sse_streams_active 0",
            "nexus_relay_completion_events_pending 0",
            "nexus_relay_completion_event_replays_total 1",
            r#"nexus_relay_completion_events_abandoned_total{reason="deadline"} 0"#,
            r#"nexus_relay_failover_decisions_total{reason="status_503",result="switched"} 1"#,
            "# TYPE nexus_relay_request_attempts histogram",
            r#"nexus_relay_request_attempts_bucket{endpoint="anthropic_messages",transport="http",le="1"} 0"#,
            r#"nexus_relay_request_attempts_bucket{endpoint="anthropic_messages",transport="http",le="2"} 1"#,
            r#"nexus_relay_request_attempts_sum{endpoint="anthropic_messages",transport="http"} 2.0"#,
        ] {
            assert!(text.contains(expected), "missing {expected} in\n{text}");
        }
        // Messages has no WebSocket transport, so no such series exists.
        assert!(!text.contains(r#"endpoint="anthropic_messages",transport="websocket""#));
        // Recording more traffic adds no series: labels come from closed sets only.
        let series = |t: &str| t.lines().filter(|l| !l.starts_with('#')).count();
        assert_eq!(
            series(&before) + 2,
            series(&text),
            "only the new control error and failover decision"
        );
        let allowed: BTreeSet<String> = Outcome::ALL
            .iter()
            .map(|o| o.as_str())
            .chain(Transport::ALL.iter().map(|t| t.as_str()))
            .chain(Dependency::ALL.iter().map(|d| d.as_str()))
            .chain(ErrorKind::ALL.iter().map(|k| k.as_str()))
            .chain(Abandoned::ALL.iter().map(|a| a.as_str()))
            .chain(FailoverReason::ALL.iter().map(|r| r.as_str()))
            .chain(FailoverResult::ALL.iter().map(|r| r.as_str()))
            .chain(["anthropic_messages", "openai_responses", "reserve"])
            .map(str::to_owned)
            .collect();
        let found = label_values(&text);
        assert!(found.is_subset(&allowed), "unexpected labels {found:?}");
    }

    #[test]
    fn a_request_tally_counts_once_and_defaults_to_rejected() {
        let count = |outcome| {
            let labels = RequestLabels {
                endpoint: Endpoint::AnthropicMessages,
                transport: Transport::Sse,
                outcome,
            };
            global().requests.get_or_create(&labels).get()
        };
        let (rejected, succeeded) = (count(Outcome::Rejected), count(Outcome::Succeeded));
        let mut tally = RequestTally::new(Endpoint::AnthropicMessages, Transport::Http);
        tally.set_transport(Transport::Sse);
        tally.deployment = Some(Uuid::new_v4());
        tally.record(Outcome::Succeeded);
        tally.record(Outcome::Unknown);
        drop(tally);
        drop(RequestTally::new(
            Endpoint::AnthropicMessages,
            Transport::Sse,
        ));
        drop(RequestTally::detached());
        assert_eq!(count(Outcome::Succeeded), succeeded + 1);
        assert_eq!(count(Outcome::Rejected), rejected + 1);
        assert_eq!(
            Outcome::from_delivery("client_cancelled"),
            Outcome::ClientCancelled
        );
        assert_eq!(Outcome::from_delivery("anything else"), Outcome::Unknown);
    }

    #[tokio::test]
    async fn metrics_listener_serves_metrics_and_liveness_only() {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let base = format!("http://{}", listener.local_addr().unwrap());
        let server = tokio::spawn(serve(listener, || NodeSample {
            revision: Some(7),
            ..NodeSample::default()
        }));
        let client = reqwest::Client::builder().no_proxy().build().unwrap();
        let response = client.get(format!("{base}/metrics")).send().await.unwrap();
        assert_eq!(response.status(), 200);
        assert_eq!(
            response.headers()[header::CONTENT_TYPE],
            "text/plain; version=0.0.4; charset=utf-8"
        );
        assert_eq!(response.headers()[header::CACHE_CONTROL], "no-store");
        let body = response.text().await.unwrap();
        assert!(body.contains("nexus_relay_snapshot_revision 7"));
        assert!(body.contains("# TYPE nexus_relay_requests_total counter"));
        let live = client.get(format!("{base}/live")).send().await.unwrap();
        assert_eq!(live.status(), 200);
        for path in ["/ready", "/v1/messages", "/v1/responses"] {
            let status = client.get(format!("{base}{path}")).send().await.unwrap();
            assert_eq!(status.status(), 404, "{path}");
        }
        let post = client.post(format!("{base}/metrics")).send().await.unwrap();
        assert_eq!(post.status(), 405);
        server.abort();
    }
}
