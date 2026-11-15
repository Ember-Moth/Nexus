use base64::{Engine, engine::general_purpose::STANDARD};
use crypto_box::SecretKey;
use nexus_relay::{
    control::{
        database::Gateway,
        limits::{Admissions, Limits},
    },
    metrics,
    server::{Node, NodeLimits, serve},
    telemetry,
    upstream::Upstream,
};
use std::{env, error::Error, process::ExitCode, time::Duration};
use tokio::net::TcpListener;
use tokio_util::sync::CancellationToken;
use tracing::{error, info};
use zeroize::Zeroizing;

fn setting<T: std::str::FromStr>(name: &str, default: T) -> Result<T, String> {
    match env::var(name) {
        Ok(s) => s.parse().map_err(|_| format!("invalid {name}")),
        Err(env::VarError::NotPresent) => Ok(default),
        Err(_) => Err(format!("invalid {name}")),
    }
}

// Startup errors are logged, so they name the variable and never echo its value.
// `<name>_FILE` may name a file holding the value instead, such as a mounted secret.
fn required(name: &str) -> Result<String, String> {
    match env::var(name) {
        Ok(value) => Ok(value),
        Err(env::VarError::NotPresent) => match env::var_os(format!("{name}_FILE")) {
            Some(path) => std::fs::read_to_string(path)
                .map(|value| value.trim_end_matches(['\r', '\n']).to_owned())
                .map_err(|_| format!("unreadable {name}_FILE")),
            None => Err(format!("missing or invalid {name}")),
        },
        Err(_) => Err(format!("missing or invalid {name}")),
    }
}

#[tokio::main]
async fn main() -> Result<ExitCode, Box<dyn Error>> {
    telemetry::init(env::var("NEXUS_RELAY_LOG").ok().as_deref())?;
    match run().await {
        Ok(()) => {
            info!("relay stopped");
            Ok(ExitCode::SUCCESS)
        }
        Err(error) => {
            error!(error = %error, "relay stopped with an error");
            Ok(ExitCode::FAILURE)
        }
    }
}

async fn run() -> Result<(), Box<dyn Error>> {
    let cluster: uuid::Uuid = required("NEXUS_RELAY_CLUSTER_ID")?
        .parse()
        .map_err(|_| "invalid NEXUS_RELAY_CLUSTER_ID")?;
    info!(
        cluster_id = %cluster,
        version = env!("CARGO_PKG_VERSION"),
        "relay starting"
    );
    let key = Zeroizing::new(
        STANDARD
            .decode(required("NEXUS_RELAY_PRIVATE_KEY")?)
            .map_err(|_| "invalid NEXUS_RELAY_PRIVATE_KEY")?,
    );
    let private = SecretKey::from_slice(&key).map_err(|_| "invalid NEXUS_RELAY_PRIVATE_KEY")?;
    let database = required("NEXUS_RELAY_DATABASE_URL")?;
    let db = Gateway::connect(
        &database,
        setting("NEXUS_RELAY_POOL_SIZE", 8)?,
        Duration::from_secs(5),
    )
    .await?;
    // The configuration refresh has a connection of its own, outside the request pool.
    let control = Gateway::connect(&database, 1, Duration::from_secs(5)).await?;
    let limits = Limits::connect(
        &required("NEXUS_RELAY_REDIS_URL")?,
        env::var("NEXUS_RELAY_NAMESPACE").unwrap_or_else(|_| "nexus:v1".into()),
    )
    .await?
    .with_admissions(Admissions::new(
        setting("NEXUS_RELAY_TENANT_ADMISSIONS", 32)?,
        Duration::from_millis(setting("NEXUS_RELAY_ADMISSION_WAIT_MS", 5000)?),
    ));
    let root = env::var_os("NEXUS_RELAY_UPSTREAM_CA")
        .map(std::fs::read)
        .transpose()?;
    let upstream = Upstream::new(
        setting("NEXUS_RELAY_RESPONSE_BYTES", 8 * 1024 * 1024)?,
        root,
    )?;
    let node = Node::new(
        db,
        control,
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
            requests_per_tenant: setting("NEXUS_RELAY_REQUESTS_PER_TENANT", 32)?,
            streams_per_tenant: setting("NEXUS_RELAY_STREAMS_PER_TENANT", 8)?,
            purge_interval: Duration::from_millis(setting(
                "NEXUS_RELAY_PURGE_INTERVAL_MS",
                60_000,
            )?),
            drain: Duration::from_secs(setting("NEXUS_RELAY_DRAIN_SECONDS", 15)?),
        },
    )?;
    let listener = TcpListener::bind(
        env::var("NEXUS_RELAY_LISTEN").unwrap_or_else(|_| "127.0.0.1:8081".into()),
    )
    .await?;
    // Metrics stay off the client listener and keep serving while the node drains.
    let metrics_listener = TcpListener::bind(
        env::var("NEXUS_RELAY_METRICS_LISTEN").unwrap_or_else(|_| "127.0.0.1:9464".into()),
    )
    .await?;
    info!(listen = %listener.local_addr()?, "relay listening");
    info!(listen = %metrics_listener.local_addr()?, "metrics listening");
    let sampled = node.clone();
    tokio::spawn(async move {
        if let Err(error) = metrics::serve(metrics_listener, move || sampled.observe()).await {
            error!(error = %error, "metrics listener stopped");
        }
    });
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
        info!("shutdown signal received");
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

#[cfg(test)]
mod tests {
    use super::required;

    #[test]
    fn secrets_can_come_from_files() {
        let dir = std::env::temp_dir().join(format!("nexus-relay-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("database-url");
        std::fs::write(&path, "postgres://node@db/nexus\n").unwrap();
        // SAFETY: this test is the only one in the binary that touches these variables.
        unsafe {
            std::env::set_var("NEXUS_RELAY_TEST_SECRET_FILE", &path);
            std::env::set_var("NEXUS_RELAY_TEST_DIRECT", "direct");
            std::env::set_var("NEXUS_RELAY_TEST_MISSING_FILE", dir.join("absent"));
        }
        assert_eq!(
            required("NEXUS_RELAY_TEST_SECRET").unwrap(),
            "postgres://node@db/nexus"
        );
        assert_eq!(required("NEXUS_RELAY_TEST_DIRECT").unwrap(), "direct");
        // Neither message echoes the path or the value.
        assert_eq!(
            required("NEXUS_RELAY_TEST_MISSING").unwrap_err(),
            "unreadable NEXUS_RELAY_TEST_MISSING_FILE"
        );
        assert_eq!(
            required("NEXUS_RELAY_TEST_UNSET").unwrap_err(),
            "missing or invalid NEXUS_RELAY_TEST_UNSET"
        );
        std::fs::remove_dir_all(dir).unwrap();
    }
}
