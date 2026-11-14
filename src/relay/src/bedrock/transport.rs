use super::wire;
use crate::{
    control::snapshot::Deployment,
    protocol::ProtocolError,
    upstream::{Reply, UpstreamError},
};
use aws_credential_types::Credentials;
use aws_sigv4::{
    http_request::{SignableBody, SignableRequest, SigningSettings, sign},
    sign::v4,
};
use http::HeaderValue;
use reqwest::{Client, Response};
use serde::Deserialize;
use serde_json::{Value, json};
use std::time::{Duration, SystemTime};

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct SigningCredential {
    access_key_id: String,
    secret_access_key: String,
    session_token: Option<String>,
}

pub async fn open(
    client: &Client,
    dep: &Deployment,
    secret: &str,
    body: &Value,
    stream: bool,
) -> Result<Response, UpstreamError> {
    let mut url = dep.base_url.clone();
    // AWS modelId is a single non-greedy URI label, including when it contains an ARN.
    const LABEL: &percent_encoding::AsciiSet = &percent_encoding::NON_ALPHANUMERIC
        .remove(b'-')
        .remove(b'.')
        .remove(b'_')
        .remove(b'~');
    let model = percent_encoding::utf8_percent_encode(&dep.upstream_model, LABEL);
    let operation = if stream {
        "converse-stream"
    } else {
        "converse"
    };
    url.set_path(&format!(
        "{}/model/{model}/{operation}",
        dep.base_url.path().trim_end_matches('/')
    ));
    let bytes = serde_json::to_vec(body)?;
    let accept = if stream {
        "application/vnd.amazon.eventstream"
    } else {
        "application/json"
    };
    let mut call = client
        .post(url.clone())
        .header("content-type", "application/json")
        .header("accept", accept);
    if secret.starts_with('{') {
        let keys: SigningCredential =
            serde_json::from_str(secret).map_err(|_| UpstreamError::Signing)?;
        let identity = Credentials::new(
            keys.access_key_id,
            keys.secret_access_key,
            keys.session_token,
            None,
            "Nexus deployment",
        )
        .into();
        let params = v4::SigningParams::builder()
            .identity(&identity)
            .region(&dep.region)
            .name("bedrock")
            .time(SystemTime::now())
            .settings(SigningSettings::default())
            .build()
            .map_err(|_| UpstreamError::Signing)?
            .into();
        let request = SignableRequest::new(
            "POST",
            url.as_str(),
            [("content-type", "application/json"), ("accept", accept)].into_iter(),
            SignableBody::Bytes(&bytes),
        )
        .map_err(|_| UpstreamError::Signing)?;
        let (instructions, _) = sign(request, &params)
            .map_err(|_| UpstreamError::Signing)?
            .into_parts();
        let (headers, _) = instructions.into_parts();
        for header in headers {
            let mut v =
                HeaderValue::from_str(header.value()).map_err(|_| UpstreamError::Signing)?;
            v.set_sensitive(header.sensitive());
            call = call.header(header.name(), v);
        }
    } else {
        call = call.bearer_auth(secret);
    }
    if !stream {
        call = call.timeout(Duration::from_secs(120));
    }
    tokio::time::timeout(Duration::from_secs(30), call.body(bytes).send())
        .await
        .map_err(|_| UpstreamError::Timeout)?
        .map_err(Into::into)
}

pub fn request_id(response: &Response) -> Option<String> {
    response
        .headers()
        .get("x-amzn-requestid")
        .and_then(|h| h.to_str().ok())
        .filter(|s| !s.is_empty() && s.len() <= 200)
        .map(str::to_owned)
}

pub fn error_body(status: http::StatusCode, body: &Value) -> Value {
    let kind = match status.as_u16() {
        400 | 422 => "invalid_request_error",
        401 => "authentication_error",
        403 => "permission_error",
        404 => "not_found_error",
        429 => "rate_limit_error",
        503 | 529 => "overloaded_error",
        _ => "api_error",
    };
    json!({"type":"error","error":{"type":kind,"message":body["message"].as_str().or_else(||body["Message"].as_str()).unwrap_or("Bedrock request failed")}})
}

pub async fn buffered(
    response: Response,
    model: &str,
    id: &str,
    limit: usize,
) -> Result<Reply, UpstreamError> {
    let status = response.status();
    let provider_id = request_id(&response);
    if status.is_redirection() {
        return Err(UpstreamError::Redirect);
    }
    let bytes = crate::upstream::read_body(response, limit).await?;
    let body: Value = serde_json::from_slice(&bytes)?;
    let usage = wire::evidence(body.get("usage"));
    let (output, status, outcome) = if !status.is_success() {
        (error_body(status, &body), status, "failed")
    } else {
        match wire::message(&body, provider_id.as_deref().unwrap_or(id), model, &usage) {
            Ok(msg) => (msg, status, "succeeded"),
            Err(ProtocolError::Unsupported(_) | ProtocolError::Invalid(_)) => (
                error_body(http::StatusCode::BAD_GATEWAY, &Value::Null),
                http::StatusCode::BAD_GATEWAY,
                "succeeded",
            ),
            Err(e) => return Err(e.into()),
        }
    };
    let body = serde_json::to_vec(&output)?;
    if body.len() > limit {
        return Err(UpstreamError::ResponseTooLarge);
    }
    Ok(Reply {
        status,
        body,
        outcome,
        response_id: provider_id,
        usage,
    })
}
