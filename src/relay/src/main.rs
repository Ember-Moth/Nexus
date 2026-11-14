use base64::{Engine, engine::general_purpose::STANDARD};
use crypto_box::SecretKey;
use nexus_relay::{
    control::{database::Gateway, limits::Limits},
    server::{Node, NodeLimits, serve},
    upstream::Upstream,
};
use std::{env, error::Error, time::Duration};
use tokio::net::TcpListener;
use tokio_util::sync::CancellationToken;
use zeroize::Zeroizing;

fn setting<T: std::str::FromStr>(name: &str, default: T) -> Result<T, String> {
    match env::var(name) {
        Ok(s) => s.parse().map_err(|_| format!("invalid {name}")),
        Err(env::VarError::NotPresent) => Ok(default),
        Err(_) => Err(format!("invalid {name}")),
    }
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn Error>> {
    let cluster = env::var("NEXUS_RELAY_CLUSTER_ID")?.parse()?;
    let key = Zeroizing::new(STANDARD.decode(env::var("NEXUS_RELAY_PRIVATE_KEY")?)?);
    let private = SecretKey::from_slice(&key)?;
    let db = Gateway::connect(
        &env::var("NEXUS_RELAY_DATABASE_URL")?,
        setting("NEXUS_RELAY_POOL_SIZE", 8)?,
        Duration::from_secs(5),
    )
    .await?;
    let limits = Limits::connect(
        &env::var("NEXUS_RELAY_REDIS_URL")?,
        env::var("NEXUS_RELAY_NAMESPACE").unwrap_or_else(|_| "nexus:v1".into()),
    )
    .await?;
    let root = env::var_os("NEXUS_RELAY_UPSTREAM_CA")
        .map(std::fs::read)
        .transpose()?;
    let upstream = Upstream::new(
        setting("NEXUS_RELAY_RESPONSE_BYTES", 8 * 1024 * 1024)?,
        root,
    )?;
    let node = Node::new(
        db,
        limits,
        private,
        upstream,
        cluster,
        NodeLimits {
            lease_ms: setting("NEXUS_RELAY_LEASE_MS", 15000)?,
            capacity: setting("NEXUS_RELAY_CONCURRENCY", 128)?,
            stream_idle: Duration::from_millis(setting("NEXUS_RELAY_STREAM_IDLE_MS", 30000)?),
            event_bytes: setting("NEXUS_RELAY_EVENT_BYTES", 1024 * 1024)?,
            sessions: setting("NEXUS_RELAY_WS_SESSIONS", 128)?,
            sessions_per_tenant: setting("NEXUS_RELAY_WS_SESSIONS_PER_TENANT", 16)?,
            turns_per_tenant: setting("NEXUS_RELAY_WS_TURNS_PER_TENANT", 16)?,
            max_attempts: setting("NEXUS_RELAY_MAX_ATTEMPTS", 2)?,
            failover_budget: Duration::from_millis(setting(
                "NEXUS_RELAY_FAILOVER_BUDGET_MS",
                10000,
            )?),
        },
    )?;
    let listener = TcpListener::bind(
        env::var("NEXUS_RELAY_LISTEN").unwrap_or_else(|_| "127.0.0.1:8081".into()),
    )
    .await?;
    let stop = CancellationToken::new();
    let signal = stop.clone();
    tokio::spawn(async move {
        #[cfg(unix)]
        {
            let mut term =
                tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
                    .expect("SIGTERM handler");
            tokio::select! {_=tokio::signal::ctrl_c()=>{},_=term.recv()=>{}}
        }
        #[cfg(not(unix))]
        let _ = tokio::signal::ctrl_c().await;
        signal.cancel();
    });
    serve(
        node,
        listener,
        setting("NEXUS_RELAY_REQUEST_BYTES", 2 * 1024 * 1024)?,
        stop,
    )
    .await
}
