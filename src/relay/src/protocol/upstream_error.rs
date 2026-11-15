//! Upstream error bodies leave the node only in a scrubbed, client-protocol shape.
//!
//! A shared provider account answers for every tenant. Its error text can carry the
//! deployment key suffix, provider organization and project IDs or AWS account ARNs,
//! and its 401/403 answers concern the deployment credential, not the caller's key.
use super::Endpoint;
use http::StatusCode;
use serde_json::{Value, json};

/// The status and body to return for an upstream error answered with `status`.
/// `body` is already in the client's protocol shape, or any JSON for resource APIs.
pub fn scrub(status: StatusCode, body: &Value, client: Endpoint) -> (StatusCode, Value) {
    if matches!(status, StatusCode::UNAUTHORIZED | StatusCode::FORBIDDEN) {
        return (
            StatusCode::SERVICE_UNAVAILABLE,
            nexus(client, "upstream_credential_unavailable"),
        );
    }
    let error = if body["error"].is_object() {
        &body["error"]
    } else {
        body
    };
    let message = error["message"]
        .as_str()
        .or_else(|| error["Message"].as_str())
        .map(redact)
        .unwrap_or_else(|| "The upstream provider returned an error.".into());
    let kind = label(&error["type"]);
    let value = match client {
        Endpoint::AnthropicMessages => json!({"type":"error","error":{
            "type": kind.unwrap_or("api_error"),
            "message": message,
        }}),
        Endpoint::OpenaiResponses => json!({"error":{
            "message": message,
            "type": kind.unwrap_or("upstream_error"),
            "param": label(&error["param"]),
            "code": label(&error["code"]),
        }}),
    };
    (status, value)
}

/// Like [`scrub`] for raw bytes that may not be JSON.
pub fn scrub_bytes(status: StatusCode, body: &[u8], client: Endpoint) -> (StatusCode, Value) {
    let value = serde_json::from_slice(body).unwrap_or(Value::Null);
    scrub(status, &value, client)
}

/// A Nexus error body with the code as its type and message.
pub fn nexus(client: Endpoint, code: &str) -> Value {
    match client {
        Endpoint::AnthropicMessages => json!({"type":"error","error":{"type":code,"message":code}}),
        Endpoint::OpenaiResponses => json!({"error":{"code":code,"type":code,"message":code}}),
    }
}

/// Redact error messages in a streamed event: Messages and Responses `error` events,
/// and the error of a failed Responses object.
pub fn scrub_event(event: &mut Value) {
    for pointer in ["/message", "/error/message", "/response/error/message"] {
        if pointer == "/message" && event["type"] != "error" {
            continue;
        }
        if let Some(text) = event.pointer_mut(pointer)
            && let Some(s) = text.as_str()
        {
            *text = redact(s).into();
        }
    }
}

/// Short protocol identifiers such as an error type, code or parameter name.
fn label(value: &Value) -> Option<&str> {
    value.as_str().filter(|s| {
        !s.is_empty()
            && s.len() <= 64
            && s.bytes()
                .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'_' | b'-' | b'.' | b'[' | b']'))
            && !sensitive(s)
    })
}

/// Replace credential fragments, provider account identifiers and addresses.
pub fn redact(message: &str) -> String {
    const LIMIT: usize = 1000;
    let mut out = String::with_capacity(message.len().min(LIMIT));
    let mut word = String::new();
    let flush = |word: &mut String, out: &mut String| {
        if sensitive(word.trim_end_matches(['.', ':', '/'])) {
            let trailing = word.len() - word.trim_end_matches(['.', ':', '/']).len();
            out.push_str("[redacted]");
            out.push_str(&word[word.len() - trailing..]);
        } else {
            out.push_str(word);
        }
        word.clear();
    };
    for c in message.chars() {
        if c.is_ascii_alphanumeric()
            || matches!(c, '_' | '-' | ':' | '.' | '/' | '*' | '@' | '+' | '=')
        {
            word.push(c);
        } else {
            flush(&mut word, &mut out);
            out.push(c);
        }
        if out.len() >= LIMIT {
            break;
        }
    }
    flush(&mut word, &mut out);
    if out.len() > LIMIT {
        let mut end = LIMIT;
        while !out.is_char_boundary(end) {
            end -= 1;
        }
        out.truncate(end);
    }
    out
}

fn sensitive(word: &str) -> bool {
    let lower = word.to_ascii_lowercase();
    let digits = word.bytes().filter(u8::is_ascii_digit).count();
    let alnum = word.bytes().filter(u8::is_ascii_alphanumeric).count();
    [
        "sk-", "sk_", "org-", "org_", "proj_", "proj-", "user-", "acct_", "arn:", "key-", "akia",
        "asia", "aiza", "ghp_", "xox",
    ]
    .iter()
    .any(|p| lower.starts_with(p) && word.len() > p.len())
        || word.contains("***")
        || word.contains('@') && word.rsplit('@').next().is_some_and(|d| d.contains('.'))
        // AWS account numbers.
        || word.len() == 12 && digits == 12
        // Long opaque tokens: keys, signatures and account-scoped identifiers.
        || alnum >= 32 && digits > 0 && !word.contains('/')
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn credential_failures_become_a_nexus_error() {
        for status in [StatusCode::UNAUTHORIZED, StatusCode::FORBIDDEN] {
            let body = json!({"error":{"message":"Incorrect API key provided: sk-proj-****abcd.",
                "type":"invalid_request_error","code":"invalid_api_key"}});
            let (s, v) = scrub(status, &body, Endpoint::OpenaiResponses);
            assert_eq!(s, StatusCode::SERVICE_UNAVAILABLE);
            assert_eq!(v["error"]["code"], "upstream_credential_unavailable");
            assert!(!v.to_string().contains("abcd"));
            let (_, v) = scrub(status, &body, Endpoint::AnthropicMessages);
            assert_eq!(v["error"]["type"], "upstream_credential_unavailable");
        }
    }

    #[test]
    fn other_errors_keep_type_and_status_but_lose_account_identifiers() {
        let body = json!({"error":{"message":"Rate limit reached for gpt-x in organization \
            org-AbCdEf123 on tokens per min. Limit 30000, Used 29000.","type":"tokens",
            "param":null,"code":"rate_limit_exceeded"}});
        let (s, v) = scrub(
            StatusCode::TOO_MANY_REQUESTS,
            &body,
            Endpoint::OpenaiResponses,
        );
        assert_eq!(s, StatusCode::TOO_MANY_REQUESTS);
        assert_eq!(v["error"]["type"], "tokens");
        assert_eq!(v["error"]["code"], "rate_limit_exceeded");
        let text = v["error"]["message"].as_str().unwrap();
        assert!(!text.contains("AbCdEf123") && text.contains("[redacted] on tokens"));
        let aws = json!({"message":"User: arn:aws:sts::123456789012:assumed-role/x/y is not \
            authorized to perform: bedrock:InvokeModel on account 123456789012."});
        let (_, v) = scrub(StatusCode::BAD_REQUEST, &aws, Endpoint::AnthropicMessages);
        let text = v["error"]["message"].as_str().unwrap();
        assert!(
            !text.contains("123456789012") && !text.contains("assumed-role"),
            "{text}"
        );
        assert_eq!(v["error"]["type"], "api_error");
        let (_, v) = scrub_bytes(
            StatusCode::BAD_GATEWAY,
            b"<html>",
            Endpoint::OpenaiResponses,
        );
        assert_eq!(v["error"]["type"], "upstream_error");
        let ok = json!({"type":"error","error":{"type":"invalid_request_error",
            "message":"max_tokens: Field required"}});
        let (_, v) = scrub(StatusCode::BAD_REQUEST, &ok, Endpoint::AnthropicMessages);
        assert_eq!(v, ok);
    }

    #[test]
    fn streamed_error_messages_are_redacted() {
        let mut event = json!({"type":"error","code":"server_error",
            "message":"contact ops@example.com about proj_abc123"});
        scrub_event(&mut event);
        assert_eq!(event["message"], "contact [redacted] about [redacted]");
        let mut failed = json!({"type":"response.failed","response":{"error":{"code":"x",
            "message":"key sk-live-1234 rejected"}}});
        scrub_event(&mut failed);
        assert_eq!(
            failed["response"]["error"]["message"],
            "key [redacted] rejected"
        );
    }
}
