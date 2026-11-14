use nexus_relay::{
    bedrock::{self, wire},
    protocol::{Endpoint, request::Generate},
};
use serde_json::json;

#[test]
fn actual_counts_are_preserved_even_when_totals_require_review() {
    let mut v = json!({"inputTokens":11,"cacheReadInputTokens":7,"cacheWriteInputTokens":5,"outputTokens":4,"totalTokens":27,
        "cacheDetails":[{"ttl":"1h","inputTokens":3},{"ttl":"5m","inputTokens":2}]});
    let e = wire::evidence(Some(&v));
    assert_eq!(e.usage_format, bedrock::FORMAT);
    assert_eq!(wire::total(&e), Some(27));
    let visible = wire::visible(&e);
    assert_eq!(visible["input_tokens"], 11);
    assert_eq!(visible["cache_creation"]["ephemeral_1h_input_tokens"], 3);
    v["totalTokens"] = 15.into();
    let bad = wire::evidence(Some(&v));
    assert_eq!(wire::total(&bad), None);
    assert_eq!(bad.raw_usage.unwrap()["total_tokens"], 15);
    v["unpricedDimension"] = 1.into();
    assert!(wire::evidence(Some(&v)).raw_usage.is_none());
    assert_eq!(
        wire::total(&wire::evidence(Some(
            &json!({"inputTokens":0,"outputTokens":0,"totalTokens":0})
        ))),
        Some(0)
    );
    assert!(wire::evidence(None).raw_usage.is_none());
}

#[test]
fn rejects_requests_with_nonportable_semantics() {
    let base = json!({"model":"m","max_tokens":4,"messages":[{"role":"user","content":"hello"}]});
    for (key, value) in [
        ("thinking", json!({"type":"enabled"})),
        ("metadata", json!({"user_id":"x"})),
        ("stop_sequences", json!(["1", "2", "3", "4", "5"])),
        (
            "tools",
            json!([{"name":"bad:name","input_schema":{"type":"object"}}]),
        ),
    ] {
        let mut body = base.clone();
        body[key] = value;
        let req = Generate::parse(Endpoint::AnthropicMessages, body, false).unwrap();
        assert!(bedrock::prepare(&req).is_err());
    }
}
