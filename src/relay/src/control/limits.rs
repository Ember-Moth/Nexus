//! Redis keys, Lua effects, and fingerprint bytes are part of the Python simulator contract.
use std::time::Duration;

use redis::{
    AsyncCommands, Script,
    aio::{ConnectionManager, ConnectionManagerConfig},
};
use serde::{Deserialize, Serialize};
use serde_json::json;
use sha2::{Digest, Sha256};
use tokio::time::Instant;
use uuid::Uuid;

use super::snapshot::{Key, Metric, Quota};
use crate::{
    metrics::{self, Dependency, ErrorKind},
    protocol::tokens::ReservationBounds,
};

#[derive(Debug, thiserror::Error)]
pub enum LimitError {
    #[error("quota_limited")]
    Limited,
    #[error("lease_expired")]
    Expired,
    #[error("quota_unavailable")]
    Unavailable,
    #[error("lease_conflict")]
    Conflict,
}

/// Count a failed Redis command for `nexus_relay_control_errors_total`.
fn unavailable(operation: &'static str) -> LimitError {
    metrics::control_error(Dependency::Redis, operation, ErrorKind::Unavailable);
    LimitError::Unavailable
}

#[derive(Clone)]
pub struct Limits {
    client: redis::Client,
    conn: ConnectionManager,
    namespace: String,
    acquire: Script,
    change: Script,
}

#[derive(Deserialize)]
struct RedisLease {
    keys: Vec<String>,
    owner: Uuid,
    deadline: u64,
    ttl: u64,
}

pub struct Lease {
    state: RedisLease,
    pub safe_until: Instant,
}

impl Lease {
    pub fn renewal_at(&self) -> Instant {
        self.safe_until - Duration::from_millis(self.state.ttl / 2)
    }
}

pub struct Admission<'a> {
    pub key: &'a Key,
    pub request: Uuid,
    pub node: Uuid,
    pub model: Uuid,
    pub revision: i64,
    pub bounds: ReservationBounds,
    pub quotas: &'a [Quota],
    pub ttl_ms: u32,
}

// All fields in the fingerprint are canonical UUIDs, integers, or protocol enum strings.
// Python json.dumps(sort_keys=True) includes one space after each separator.
fn fingerprint(binding: serde_json::Value) -> String {
    struct PythonJson;
    impl serde_json::ser::Formatter for PythonJson {
        fn begin_array_value<W: ?Sized + std::io::Write>(
            &mut self,
            w: &mut W,
            first: bool,
        ) -> std::io::Result<()> {
            if first { Ok(()) } else { w.write_all(b", ") }
        }
        fn begin_object_key<W: ?Sized + std::io::Write>(
            &mut self,
            w: &mut W,
            first: bool,
        ) -> std::io::Result<()> {
            if first { Ok(()) } else { w.write_all(b", ") }
        }
        fn begin_object_value<W: ?Sized + std::io::Write>(
            &mut self,
            w: &mut W,
        ) -> std::io::Result<()> {
            w.write_all(b": ")
        }
    }
    let mut bytes = Vec::new();
    binding
        .serialize(&mut serde_json::Serializer::with_formatter(
            &mut bytes, PythonJson,
        ))
        .expect("serializing a JSON value to memory");
    format!("{:x}", Sha256::digest(bytes))
}

impl Limits {
    pub async fn connect(url: &str, namespace: String) -> Result<Self, LimitError> {
        let client = redis::Client::open(url).map_err(|_| LimitError::Unavailable)?;
        let config = ConnectionManagerConfig::new()
            .set_connection_timeout(Duration::from_secs(3))
            .set_response_timeout(Duration::from_secs(3));
        let conn = client
            .get_connection_manager_with_config(config)
            .await
            .map_err(|_| LimitError::Unavailable)?;
        Ok(Self {
            client,
            conn,
            namespace,
            acquire: Script::new(include_str!("lua/acquire.lua")),
            change: Script::new(include_str!("lua/change.lua")),
        })
    }

    pub async fn acquire(&self, a: Admission<'_>) -> Result<Lease, LimitError> {
        if !(100..=60000).contains(&a.ttl_ms) {
            return Err(LimitError::Unavailable);
        }
        let org = a.key.organization_id;
        let upper = u64::from(a.bounds.inputs) + u64::from(a.bounds.outputs);
        let mut policies: Vec<_> = a
            .quotas
            .iter()
            .filter(|p| p.organization_id == org && p.scope_id == a.key.scope(p.scope_type))
            .collect();
        policies.sort_by_key(|p| p.id);
        let fp = fingerprint(json!([
            org,
            a.key.project_id,
            a.key.id,
            upper,
            policies,
            a.ttl_ms,
            [
                a.node,
                a.model,
                a.bounds.inputs,
                a.bounds.outputs,
                a.revision.to_string()
            ]
        ]));
        let specs = serde_json::to_string(&policies).map_err(|_| LimitError::Unavailable)?;
        let owner = Uuid::new_v4();
        let mut conn = self.conn.clone();
        for _ in 0..3 {
            let now = Instant::now();
            let (secs, _): (u64, u64) = redis::cmd("TIME")
                .query_async(&mut conn)
                .await
                .map_err(|_| unavailable("acquire"))?;
            let window = secs / 60;
            let mut script = self.acquire.prepare_invoke();
            script.key(format!("{}:lease:{{{org}}}:{}", self.namespace, a.request));
            for p in &policies {
                let suffix = format!("{}:{}", p.id, p.metric.name());
                let key = match p.metric {
                    Metric::Concurrency => {
                        format!("{}:concurrency:{{{org}}}:{suffix}", self.namespace)
                    }
                    _ => format!("{}:limit:{{{org}}}:{suffix}:{window}", self.namespace),
                };
                script.key(key);
            }
            script
                .key(format!("{}:limit:{{{org}}}:epoch", self.namespace))
                .arg(&fp)
                .arg(window)
                .arg(upper)
                .arg(a.ttl_ms)
                .arg(&specs)
                .arg(owner.to_string());
            let result: String = match script.invoke_async(&mut conn).await {
                Ok(v) => v,
                Err(e) if e.is_io_error() => {
                    unavailable("acquire");
                    continue;
                }
                Err(_) => return Err(unavailable("acquire")),
            };
            match result.as_str() {
                "retry_window" => continue,
                "expired" => return Err(LimitError::Expired),
                "conflict" => return Err(LimitError::Conflict),
                "limited" => return Err(LimitError::Limited),
                "recovering" => return Err(LimitError::Unavailable),
                _ => {}
            }
            let state: RedisLease =
                serde_json::from_str(&result).map_err(|_| unavailable("acquire"))?;
            let (secs, micros): (u64, u64) = redis::cmd("TIME")
                .query_async(&mut conn)
                .await
                .map_err(|_| unavailable("acquire"))?;
            let safe_until = now
                + Duration::from_micros(
                    (state.deadline * 1000).saturating_sub(secs * 1_000_000 + micros),
                );
            return Ok(Lease { state, safe_until });
        }
        Err(LimitError::Unavailable)
    }

    async fn change(
        &self,
        lease: &Lease,
        operation: &'static str,
        actual: &str,
    ) -> Result<(), LimitError> {
        let mut cmd = self.change.prepare_invoke();
        for key in &lease.state.keys {
            cmd.key(key);
        }
        let ok: bool = cmd
            .arg(lease.state.owner.to_string())
            .arg(operation)
            .arg(actual)
            .invoke_async(&mut self.conn.clone())
            .await
            .map_err(|_| unavailable(operation))?;
        if !ok {
            return Err(LimitError::Expired);
        }
        Ok(())
    }

    pub async fn renew(&self, lease: &mut Lease) -> Result<(), LimitError> {
        let started = Instant::now();
        if started >= lease.safe_until {
            return Err(LimitError::Expired);
        }
        tokio::time::timeout_at(lease.safe_until, self.change(lease, "renew", ""))
            .await
            .map_err(|_| LimitError::Expired)??;
        lease.safe_until = started + Duration::from_millis(lease.state.ttl);
        Ok(())
    }

    pub async fn finish(&self, lease: &Lease, actual: Option<u64>) -> Result<(), LimitError> {
        let actual = actual
            .map(|n| n.to_string())
            .unwrap_or_else(|| "unknown".into());
        self.change(lease, "finish", &actual).await
    }

    pub async fn notifications(
        &self,
        cluster: Uuid,
    ) -> Result<redis::aio::PubSubStream, LimitError> {
        let mut sub = self
            .client
            .get_async_pubsub()
            .await
            .map_err(|_| unavailable("subscribe"))?;
        sub.subscribe(format!("{}:cfg:{cluster}:changed", self.namespace))
            .await
            .map_err(|_| unavailable("subscribe"))?;
        Ok(sub.into_on_message())
    }

    pub async fn heartbeat(&self, node: Uuid, revision: i64) -> Result<(), LimitError> {
        let key = format!("{}:node:{node}", self.namespace);
        let body =
            json!({"revision":revision.to_string(),"seen_at":chrono::Utc::now().to_rfc3339()});
        self.conn
            .clone()
            .set_ex(key, body.to_string(), 30)
            .await
            .map_err(|_| unavailable("heartbeat"))
    }
}
