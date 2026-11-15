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
/// must also classify the provider's error envelope with [`refusal`].
pub fn declined(status: StatusCode) -> bool {
    matches!(status.as_u16(), 429 | 503 | 529)
}

/// The error contract of the upstream that answered.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ErrorContract {
    /// `{"type":"error","error":{"type":…}}` from Anthropic Messages.
    Anthropic,
    /// `{"error":{"type":…,"code":…}}` from OpenAI Responses and Chat Completions.
    OpenAi,
    /// The AWS service error name in `x-amzn-ErrorType`, before Nexus re-encodes the body.
    Bedrock,
}

/// What a 429, 503 or 529 answer proves about the generation.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Refusal {
    /// The vendor documents this error as an admission rejection: nothing was executed,
    /// so another deployment may receive a new attempt.
    Declined,
    /// A 429 in the vendor's error envelope whose type is not documented. HTTP 429 refuses
    /// the request and nothing was delivered, so the customer reservation is released. Without
    /// a documented type it is not proof enough to send the POST again.
    Refused,
    /// No proof either way, for example an intermediary's 502 or OpenAI's 503 `server_error`.
    Unknown,
}

// Anthropic: https://platform.claude.com/docs/en/api/errors documents 429 rate_limit_error
// (rate, tier spend cap, workspace spend limit) and 529 overloaded_error. Overloaded errors are
// also accepted with 503, as the SSE error event uses the same type.
// OpenAI: https://developers.openai.com/api/docs/guides/error-codes and
// https://developers.openai.com/api/docs/guides/rate-limits document the 429 codes below and
// 503 service_unavailable_error/server_is_overloaded ("Model temporarily overloaded").
// Bedrock: https://docs.aws.amazon.com/bedrock/latest/APIReference/API_runtime_Converse.html
// (and InvokeModel, ConverseStream) document 429 ThrottlingException and ModelNotReadyException
// and 503 ServiceUnavailableException.
// `rate_limit_exceeded` is the code OpenAI sends for "Rate limit reached for requests"; the guide
// lists that row without a code string. `insufficient_quota` is documented as an error type.
const OPENAI_429: [&str; 8] = [
    "rate_limit_exceeded",
    "rate_limit_error",
    "slow_down",
    "insufficient_quota",
    "credit_balance_exhausted",
    "organization_spend_limit_exceeded",
    "project_spend_limit_exceeded",
    "organization_usage_limit_exceeded",
];

pub fn refusal(
    contract: ErrorContract,
    status: StatusCode,
    aws_error_type: Option<&str>,
    body: &[u8],
) -> Refusal {
    let status = status.as_u16();
    let (documented, enveloped) = match contract {
        ErrorContract::Bedrock => {
            let Some(name) = aws_error_type.map(|v| v.split(':').next().unwrap_or(v)) else {
                return Refusal::Unknown;
            };
            let documented = matches!(
                (status, name),
                (429, "ThrottlingException" | "ModelNotReadyException")
                    | (503, "ServiceUnavailableException")
            );
            (documented, !name.is_empty())
        }
        ErrorContract::Anthropic => {
            let Ok(value) = serde_json::from_slice::<Value>(body) else {
                return Refusal::Unknown;
            };
            let kind = value["error"]["type"].as_str();
            let documented = match status {
                429 => kind == Some("rate_limit_error"),
                503 | 529 => kind == Some("overloaded_error"),
                _ => false,
            };
            (documented, value["type"] == "error" && kind.is_some())
        }
        ErrorContract::OpenAi => {
            let Ok(value) = serde_json::from_slice::<Value>(body) else {
                return Refusal::Unknown;
            };
            let error = &value["error"];
            let named = |field: &str| error[field].as_str().unwrap_or_default();
            let documented = match status {
                429 => OPENAI_429.contains(&named("code")) || OPENAI_429.contains(&named("type")),
                503 => {
                    named("code") == "server_is_overloaded"
                        && named("type") == "service_unavailable_error"
                }
                _ => false,
            };
            (documented, error.is_object())
        }
    };
    if documented {
        Refusal::Declined
    } else if status == 429 && enveloped {
        Refusal::Refused
    } else {
        Refusal::Unknown
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
