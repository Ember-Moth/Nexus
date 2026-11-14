use crate::protocol::{Endpoint, response, usage::UsageEvidence};
use http::StatusCode;
use reqwest::{Client, Url};
use serde_json::Value;
use std::time::Duration;

pub struct Upstream {
    client: Client,
    max_bytes: usize,
    tls: std::sync::Arc<rustls::ClientConfig>,
}
pub struct Reply {
    pub status: StatusCode,
    pub body: Vec<u8>,
    pub outcome: &'static str,
    pub response_id: Option<String>,
    pub usage: UsageEvidence,
}

impl Upstream {
    pub(crate) fn response_limit(&self) -> usize {
        self.max_bytes
    }

    pub fn new(
        max_bytes: usize,
        root: Option<Vec<u8>>,
    ) -> Result<Self, Box<dyn std::error::Error>> {
        let mut builder = Client::builder()
            .redirect(reqwest::redirect::Policy::none())
            .retry(reqwest::retry::never())
            .pool_max_idle_per_host(8)
            .no_proxy()
            .https_only(true)
            .connect_timeout(Duration::from_secs(5));
        use rustls::pki_types::{CertificateDer, pem::PemObject};
        let mut roots = rustls::RootCertStore::empty();
        roots.add_parsable_certificates(rustls_native_certs::load_native_certs().certs);
        if let Some(bytes) = root {
            for cert in CertificateDer::pem_slice_iter(&bytes) {
                roots.add(cert?)?;
            }
            builder = builder.add_root_certificate(reqwest::Certificate::from_pem(&bytes)?);
        }
        let tls = rustls::ClientConfig::builder_with_provider(std::sync::Arc::new(
            rustls::crypto::ring::default_provider(),
        ))
        .with_safe_default_protocol_versions()?
        .with_root_certificates(roots)
        .with_no_client_auth();
        Ok(Self {
            client: builder.build()?,
            max_bytes,
            tls: std::sync::Arc::new(tls),
        })
    }

    pub(crate) async fn websocket(
        &self,
        base: &Url,
        secret: &str,
        limit: usize,
    ) -> Result<UpstreamSocket, ()> {
        use tokio_tungstenite::{
            Connector, connect_async_tls_with_config,
            tungstenite::{client::IntoClientRequest, protocol::WebSocketConfig},
        };
        let mut url = Self::endpoint(base, Endpoint::OpenaiResponses);
        url.set_scheme("wss").map_err(|_| ())?;
        let mut req = url.as_str().into_client_request().map_err(|_| ())?;
        req.headers_mut().insert(
            "authorization",
            format!("Bearer {secret}").parse().map_err(|_| ())?,
        );
        let config = WebSocketConfig::default()
            .max_message_size(Some(limit))
            .max_frame_size(Some(limit))
            .write_buffer_size(0)
            .max_write_buffer_size(limit + 1024);
        let (ws, _) = tokio::time::timeout(
            Duration::from_secs(10),
            connect_async_tls_with_config(
                req,
                Some(config),
                true,
                Some(Connector::Rustls(self.tls.clone())),
            ),
        )
        .await
        .map_err(|_| ())?
        .map_err(|_| ())?;
        Ok(ws)
    }
    pub(crate) fn endpoint(base: &Url, endpoint: Endpoint) -> Url {
        let mut url = base.clone();
        let path = url.path().trim_end_matches('/');
        let prefix = if path.ends_with("/v1") {
            path.to_string()
        } else {
            format!("{path}/v1")
        };
        let resource = match endpoint {
            Endpoint::AnthropicMessages => "messages",
            Endpoint::OpenaiResponses => "responses",
        };
        url.set_path(&format!("{prefix}/{resource}"));
        url
    }
    pub(crate) async fn open(
        &self,
        base: &Url,
        endpoint: Endpoint,
        secret: &str,
        body: &Value,
        stream: bool,
    ) -> Result<reqwest::Response, ()> {
        let mut call = self.client.post(Self::endpoint(base, endpoint));
        call = match endpoint {
            Endpoint::AnthropicMessages => call
                .header("x-api-key", secret)
                .header("anthropic-version", "2023-06-01"),
            Endpoint::OpenaiResponses => call.bearer_auth(secret),
        };
        if !stream {
            call = call.timeout(Duration::from_secs(120));
        }
        tokio::time::timeout(
            Duration::from_secs(30),
            call.header(
                "accept",
                if stream {
                    "text/event-stream"
                } else {
                    "application/json"
                },
            )
            .json(body)
            .send(),
        )
        .await
        .map_err(|_| ())?
        .map_err(|_| ())
    }
    pub async fn generate(
        &self,
        base: &Url,
        endpoint: Endpoint,
        secret: &str,
        body: &Value,
    ) -> Result<Reply, ()> {
        let res = self.open(base, endpoint, secret, body, false).await?;
        self.buffered(res, endpoint).await
    }
    pub(crate) async fn buffered(
        &self,
        mut res: reqwest::Response,
        endpoint: Endpoint,
    ) -> Result<Reply, ()> {
        let status = res.status();
        if res
            .content_length()
            .is_some_and(|n| n > self.max_bytes as u64)
            || res
                .headers()
                .get("content-encoding")
                .is_some_and(|h| h != "identity")
        {
            return Err(());
        }
        let mut bytes = Vec::new();
        while let Some(chunk) = res.chunk().await.map_err(|_| ())? {
            if chunk.len() > self.max_bytes - bytes.len() {
                return Err(());
            }
            bytes.extend_from_slice(&chunk);
        }
        let mut data: Value = serde_json::from_slice(&bytes).map_err(|_| ())?;
        // Preserve the delivery fact even if a new or malformed meter cannot be billed.
        let done = match response::observe(endpoint, &data) {
            Ok(done) => done,
            Err(_) if data.get("usage").is_some() => {
                data.as_object_mut().ok_or(())?.remove("usage");
                response::observe(endpoint, &data).map_err(|_| ())?
            }
            Err(_) => return Err(()),
        };
        let outcome = if status.is_success() || done.outcome(endpoint) == "failed" {
            done.outcome(endpoint)
        } else {
            "unknown"
        };
        if status.is_redirection() {
            return Err(());
        }
        Ok(Reply {
            status,
            body: bytes,
            outcome,
            response_id: done.response_id,
            usage: done.usage,
        })
    }
}

pub(crate) type UpstreamSocket =
    tokio_tungstenite::WebSocketStream<tokio_tungstenite::MaybeTlsStream<tokio::net::TcpStream>>;
