//! Redis keys, Lua effects, and fingerprint bytes are part of the Python simulator contract.
use std::{
    collections::HashMap,
    future::Future,
    sync::{Arc, Mutex},
    time::Duration,
};

use redis::{
    AsyncCommands, Script,
    aio::{ConnectionManager, ConnectionManagerConfig},
};
use serde::{Deserialize, Serialize};
use serde_json::json;
use sha2::{Digest, Sha256};
use tokio::time::Instant;
use uuid::Uuid;

use super::{
    ControlError,
    snapshot::{Key, Layer, Metric, Quota, Scope},
};
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
    admit: Script,
    admissions: Admissions,
}

/// Cluster-wide admission slots per organization: how many of its requests may be inside the
/// database admission (reserve + first attempt, or a later attempt) at once, across all nodes.
/// Waiting happens here, before a database connection is taken, so a hot organization cannot
/// fill the connection pools with sessions queued on its serial section.
#[derive(Clone)]
pub struct Admissions {
    /// Slots of an organization without an `admission_concurrency` policy; 0 disables the gate.
    pub default: usize,
    /// How long a request may wait for a slot before it is refused with 429.
    pub wait: Duration,
    /// Waiters of this node, one queue per organization: only the head polls Redis.
    queues: Arc<Mutex<HashMap<Uuid, Arc<tokio::sync::Mutex<()>>>>>,
}

impl Admissions {
    pub fn new(default: usize, wait: Duration) -> Self {
        Self {
            default,
            wait,
            queues: Arc::default(),
        }
    }
    fn queue(&self, org: Uuid) -> Arc<tokio::sync::Mutex<()>> {
        self.queues.lock().unwrap().entry(org).or_default().clone()
    }
    fn release_queue(&self, org: Uuid) {
        let mut queues = self.queues.lock().unwrap();
        if queues.get(&org).is_some_and(|q| Arc::strong_count(q) == 1) {
            queues.remove(&org);
        }
    }
}

/// Seconds a slot survives a node that never released it (longer than any admission call).
const ADMISSION_TTL_MS: u64 = 20_000;
const ADMIT: &str = r"
local t=redis.call('TIME'); local now=tonumber(t[1])*1000+math.floor(tonumber(t[2])/1000)
redis.call('ZREMRANGEBYSCORE',KEYS[1],'-inf',now)
if redis.call('ZCARD',KEYS[1])>=tonumber(ARGV[1]) then return 0 end
redis.call('ZADD',KEYS[1],now+tonumber(ARGV[2]),ARGV[3])
redis.call('PEXPIRE',KEYS[1],tonumber(ARGV[2]))
return 1
";

/// A held admission slot; dropping it releases the slot.
struct Slot {
    conn: ConnectionManager,
    key: String,
    owner: String,
}
impl Drop for Slot {
    fn drop(&mut self) {
        let (mut conn, key, owner) = (
            self.conn.clone(),
            std::mem::take(&mut self.key),
            std::mem::take(&mut self.owner),
        );
        tokio::spawn(async move {
            let _: Result<i64, _> = conn.zrem(key, owner).await;
        });
    }
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
            admit: Script::new(ADMIT),
            admissions: Admissions::new(32, Duration::from_secs(5)),
        })
    }

    pub fn with_admissions(mut self, admissions: Admissions) -> Self {
        self.admissions = admissions;
        self
    }

    /// The organization's cluster-wide admission slots: its platform `admission_concurrency`
    /// policy, else the node default.
    pub fn admission_limit(&self, org: Uuid, quotas: &[Quota]) -> usize {
        quotas
            .iter()
            .filter(|q| {
                matches!(q.metric, Metric::AdmissionConcurrency)
                    && q.organization_id == org
                    && q.layer == Layer::Platform
                    && q.scope_type == Scope::Organization
                    && q.scope_id == org
            })
            .map(|q| q.limit_value as usize)
            .min()
            .unwrap_or(self.admissions.default)
    }

    /// Run `work`, one database admission of `org`, inside one of its `limit` cluster-wide
    /// slots. A limit of 0 runs it directly. Refused with `ControlError::Saturated` when no
    /// slot frees up within the configured wait.
    pub async fn admitted<T>(
        &self,
        org: Uuid,
        limit: usize,
        work: impl Future<Output = Result<T, ControlError>>,
    ) -> Result<T, ControlError> {
        if limit == 0 {
            return work.await;
        }
        let deadline = Instant::now() + self.admissions.wait;
        let slot = self.slot(org, limit, deadline).await;
        self.admissions.release_queue(org);
        let _slot = slot?;
        work.await
    }

    async fn slot(&self, org: Uuid, limit: usize, deadline: Instant) -> Result<Slot, ControlError> {
        let queue = self.admissions.queue(org);
        let _turn = tokio::time::timeout_at(deadline, queue.lock())
            .await
            .map_err(|_| ControlError::Saturated)?;
        let slot = Slot {
            conn: self.conn.clone(),
            key: format!("{}:admission:{{{org}}}", self.namespace),
            owner: Uuid::new_v4().to_string(),
        };
        let mut backoff = Duration::from_millis(1);
        loop {
            let admitted: i64 = self
                .admit
                .key(&slot.key)
                .arg(limit)
                .arg(ADMISSION_TTL_MS)
                .arg(&slot.owner)
                .invoke_async(&mut self.conn.clone())
                .await
                .map_err(|_| {
                    unavailable("admit");
                    ControlError::Invalid("admission slots")
                })?;
            if admitted == 1 {
                return Ok(slot);
            }
            let jitter = Duration::from_micros(rand::random::<u64>() % 1000);
            if Instant::now() + backoff >= deadline {
                // Nothing was taken; the drop below only issues a harmless ZREM.
                return Err(ControlError::Saturated);
            }
            tokio::time::sleep(backoff + jitter).await;
            backoff = (backoff * 2).min(Duration::from_millis(8));
        }
    }

    pub async fn acquire(&self, a: Admission<'_>) -> Result<Lease, LimitError> {
        let upper = u64::from(a.bounds.inputs) + u64::from(a.bounds.outputs);
        self.acquire_weighted(a, 1, upper).await
    }

    pub(crate) async fn acquire_weighted(
        &self,
        a: Admission<'_>,
        requests: usize,
        upper: u64,
    ) -> Result<Lease, LimitError> {
        if !(100..=60000).contains(&a.ttl_ms) {
            return Err(LimitError::Unavailable);
        }
        let org = a.key.organization_id;
        let mut policies: Vec<_> = a
            .quotas
            .iter()
            .filter(|p| {
                p.metric.leased()
                    && p.organization_id == org
                    && p.scope_id == a.key.scope(p.scope_type)
            })
            .collect();
        policies.sort_by_key(|p| p.id);
        let mut binding = json!([
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
        ]);
        if a.bounds.execution_mode != crate::protocol::execution::ExecutionMode::None {
            binding.as_array_mut().unwrap().push(
                json!({"execution":a.bounds.execution_mode,"seconds":a.bounds.execution_seconds}),
            );
        }
        if let Some(t) = a.bounds.interpreter {
            binding.as_array_mut().unwrap().push(json!({"container":t}));
        }
        if a.bounds.guardrail_units > 0 {
            binding
                .as_array_mut()
                .unwrap()
                .push(json!({"guardrail_unit_limit":a.bounds.guardrail_units}));
        }
        if a.bounds.file_searches > 0 {
            binding
                .as_array_mut()
                .unwrap()
                .push(json!({"file_search_limit":a.bounds.file_searches}));
        }
        if a.bounds.searches > 0 {
            binding
                .as_array_mut()
                .unwrap()
                .push(json!({"web_search_limit":a.bounds.searches}));
        }
        if requests > 1 {
            binding.as_array_mut().unwrap().push(json!(requests));
        }
        let fp = fingerprint(binding);
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
                .arg(owner.to_string())
                .arg(requests);
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
