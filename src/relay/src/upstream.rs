use crate::protocol::{
    Endpoint, ProtocolError,
    request::{AnthropicHeaders, Operation},
    response,
    usage::UsageEvidence,
};
use http::StatusCode;
use reqwest::{Client, Url};
use serde_json::Value;
use std::time::Duration;
use thiserror::Error;

#[derive(Debug, Error)]
pub enum UpstreamError {
    #[error("Bedrock request signing failed")]
    Signing,
    #[error("upstream connection could not be established")]
    Connect,
    #[error("upstream request timed out")]
    Timeout,
    #[error("upstream HTTP transport failed")]
    Transport,
    #[error("upstream response exceeds the configured size limit")]
    ResponseTooLarge,
    #[error("unsupported upstream content encoding")]
    UnsupportedEncoding,
    #[error("invalid upstream JSON")]
    InvalidJson(#[from] serde_json::Error),
    #[error(transparent)]
    Protocol(#[from] ProtocolError),
    #[error("unexpected upstream redirect")]
    Redirect,
}

impl From<reqwest::Error> for UpstreamError {
    fn from(error: reqwest::Error) -> Self {
        // A connector failure (DNS, TCP, TLS or the connect timeout) precedes the request bytes.
        if error.is_connect() {
            Self::Connect
        } else if error.is_timeout() {
            Self::Timeout
        } else {
            Self::Transport
        }
    }
}

impl UpstreamError {
    /// True only when the generation provably never reached the upstream.
    /// Any timeout or transport error after a connection was established may have executed it.
    pub fn unsent(&self) -> bool {
        matches!(self, Self::Connect | Self::Signing)
    }
}

/// These statuses can indicate an explicit admission rejection. The caller
/// must also check the provider's error envelope before issuing another POST.
pub fn declined(status: StatusCode) -> bool {
    matches!(status.as_u16(), 429 | 503 | 529)
}

pub(crate) fn confirms_rejection(endpoint: Endpoint, status: StatusCode, body: &[u8]) -> bool {
    let Ok(value) = serde_json::from_slice::<Value>(body) else {
        return false;
    };
    let error = &value["error"];
    match (endpoint, status.as_u16()) {
        (Endpoint::AnthropicMessages, 503 | 529) => {
            value["type"] == "error" && error["type"] == "overloaded_error"
        }
        (Endpoint::AnthropicMessages, 429) => {
            value["type"] == "error" && error["type"] == "rate_limit_error"
        }
        (Endpoint::OpenaiResponses, 429) => {
            error["code"] == "rate_limit_exceeded" || error["type"] == "rate_limit_exceeded"
        }
        _ => false,
    }
}

pub struct Upstream {
    pub(crate) client: Client,
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
        let mut url = Self::endpoint(base, Endpoint::OpenaiResponses, Operation::Generate);
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
    pub(crate) fn endpoint(base: &Url, endpoint: Endpoint, operation: Operation) -> Url {
        let resource = match endpoint {
            Endpoint::AnthropicMessages if operation == Operation::CountTokens => {
                "messages/count_tokens"
            }
            Endpoint::AnthropicMessages => "messages",
            Endpoint::OpenaiResponses if operation == Operation::Compact => "responses/compact",
            Endpoint::OpenaiResponses => "responses",
        };
        Self::resource(base, resource)
    }
    pub(crate) fn resource(base: &Url, resource: &str) -> Url {
        let mut url = base.clone();
        let path = url.path().trim_end_matches('/');
        let prefix = if path.ends_with("/v1") {
            path.to_string()
        } else {
            format!("{path}/v1")
        };
        url.set_path(&format!("{prefix}/{resource}"));
        url
    }
    #[allow(clippy::too_many_arguments)]
    pub(crate) async fn open(
        &self,
        url: Url,
        endpoint: Endpoint,
        secret: &str,
        body: &Value,
        stream: bool,
        anthropic: &AnthropicHeaders,
    ) -> Result<reqwest::Response, UpstreamError> {
        let mut call = self.client.post(url);
        call = match endpoint {
            Endpoint::AnthropicMessages => anthropic.apply(call.header("x-api-key", secret)),
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
        .map_err(|_| UpstreamError::Timeout)?
        .map_err(UpstreamError::from)
    }
    pub async fn generate(
        &self,
        base: &Url,
        endpoint: Endpoint,
        secret: &str,
        body: &Value,
    ) -> Result<Reply, UpstreamError> {
        let res = self
            .open(
                Self::endpoint(base, endpoint, Operation::Generate),
                endpoint,
                secret,
                body,
                false,
                &AnthropicHeaders::default(),
            )
            .await?;
        self.buffered(
            res,
            endpoint,
            Operation::Generate,
            crate::protocol::hosted::SearchMeters::for_body(body),
        )
        .await
    }
    pub(crate) async fn buffered(
        &self,
        res: reqwest::Response,
        endpoint: Endpoint,
        operation: Operation,
        search_metered: crate::protocol::hosted::SearchMeters,
    ) -> Result<Reply, UpstreamError> {
        let status = res.status();
        let bytes = read_body(res, self.max_bytes).await?;
        let mut data: Value = serde_json::from_slice(&bytes)?;
        if endpoint == Endpoint::OpenaiResponses
            && operation == Operation::Generate
            && status.is_success()
            && data["object"] == "response"
            && matches!(data["status"].as_str(), Some("queued" | "in_progress"))
        {
            let id = data["id"]
                .as_str()
                .filter(|s| !s.is_empty() && s.len() <= 200)
                .ok_or(ProtocolError::Invalid("response ID"))?
                .to_owned();
            return Ok(Reply {
                status,
                body: bytes,
                outcome: "pending",
                response_id: Some(id),
                usage: UsageEvidence::extract(endpoint, None)?,
            });
        }
        // Preserve the delivery fact even if a new or malformed meter cannot be billed.
        let done = match response::observe_operation(endpoint, operation, &data, search_metered) {
            Ok(done) => done,
            Err(error) if data.get("usage").is_some() => {
                data.as_object_mut().ok_or(error)?.remove("usage");
                response::observe_operation(endpoint, operation, &data, search_metered)?
            }
            Err(error) => return Err(error.into()),
        };
        let outcome = if status.is_success() || done.outcome(endpoint) == "failed" {
            done.outcome(endpoint)
        } else {
            "unknown"
        };
        if status.is_redirection() {
            return Err(UpstreamError::Redirect);
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

pub(crate) async fn read_body(
    mut res: reqwest::Response,
    limit: usize,
) -> Result<Vec<u8>, UpstreamError> {
    if res.content_length().is_some_and(|n| n > limit as u64) {
        return Err(UpstreamError::ResponseTooLarge);
    }
    if res
        .headers()
        .get("content-encoding")
        .is_some_and(|h| h != "identity")
    {
        return Err(UpstreamError::UnsupportedEncoding);
    }
    let mut bytes = Vec::new();
    while let Some(chunk) = res.chunk().await? {
        if chunk.len() > limit - bytes.len() {
            return Err(UpstreamError::ResponseTooLarge);
        }
        bytes.extend_from_slice(&chunk);
    }
    Ok(bytes)
}
