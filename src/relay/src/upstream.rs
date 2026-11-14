use crate::protocol::{Endpoint, response, stream::Terminal, usage::UsageEvidence};
use http::StatusCode;
use reqwest::{Client, Url};
use serde_json::Value;
use std::time::Duration;

pub struct Upstream {
    client: Client,
    max_bytes: usize,
}
pub struct Reply {
    pub status: StatusCode,
    pub body: Vec<u8>,
    pub outcome: &'static str,
    pub response_id: Option<String>,
    pub usage: UsageEvidence,
}

impl Upstream {
    pub fn new(
        max_bytes: usize,
        root: Option<reqwest::Certificate>,
    ) -> Result<Self, reqwest::Error> {
        let mut builder = Client::builder()
            .redirect(reqwest::redirect::Policy::none())
            .retry(reqwest::retry::never())
            .pool_max_idle_per_host(8)
            .no_proxy()
            .https_only(true)
            .connect_timeout(Duration::from_secs(5))
            .timeout(Duration::from_secs(120));
        if let Some(root) = root {
            builder = builder.add_root_certificate(root);
        }
        Ok(Self {
            client: builder.build()?,
            max_bytes,
        })
    }

    pub async fn generate(
        &self,
        base: &Url,
        endpoint: Endpoint,
        secret: &str,
        body: &Value,
    ) -> Result<Reply, ()> {
        let mut url = base.clone();
        let path = url.path().trim_end_matches('/');
        let prefix = if path.ends_with("/v1") {
            path.to_owned()
        } else {
            format!("{path}/v1")
        };
        let mut call = match endpoint {
            Endpoint::AnthropicMessages => {
                url.set_path(&format!("{prefix}/messages"));
                self.client
                    .post(url)
                    .header("x-api-key", secret)
                    .header("anthropic-version", "2023-06-01")
            }
            Endpoint::OpenaiResponses => {
                url.set_path(&format!("{prefix}/responses"));
                self.client.post(url).bearer_auth(secret)
            }
        };
        call = call.header("accept", "application/json").json(body);
        let mut res = call.send().await.map_err(|_| ())?;
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
        let outcome = match (status.is_success(), done.terminal) {
            (_, Terminal::Failed) => "failed",
            (true, Terminal::Completed)
                if endpoint == Endpoint::OpenaiResponses
                    || matches!(
                        done.stop_reason.as_deref(),
                        Some(
                            "end_turn"
                                | "max_tokens"
                                | "stop_sequence"
                                | "tool_use"
                                | "pause_turn"
                                | "refusal"
                                | "model_context_window_exceeded"
                        )
                    ) =>
            {
                "succeeded"
            }
            (true, Terminal::Incomplete)
                if done.stop_reason.as_deref() == Some("max_output_tokens") =>
            {
                "succeeded"
            }
            _ => "unknown",
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
