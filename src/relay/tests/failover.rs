//! Failover candidate selection and the classification of provably unexecuted attempts.
use std::collections::HashSet;

use http::StatusCode;
use nexus_relay::{
    control::{
        database::Snapshot,
        snapshot::{Index, Selection},
    },
    protocol::{Endpoint, request::Generate, tokens::ReservationBounds},
    upstream::{DECLINED, ErrorContract, Refusal, Upstream, UpstreamError, declined, refusal},
};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use tokio::net::TcpListener;
use uuid::Uuid;

struct Fixture {
    index: Index,
    ids: Vec<Uuid>,
}

/// Build one route over `candidates`: (priority, weight, relay profile overrides).
fn fixture(candidates: &[(u32, u32, Value)]) -> Fixture {
    build(candidates).unwrap()
}

fn build(candidates: &[(u32, u32, Value)]) -> Result<Fixture, nexus_relay::control::ControlError> {
    let (org, key, project, model, credential) = (
        Uuid::new_v4(),
        Uuid::new_v4(),
        Uuid::new_v4(),
        Uuid::new_v4(),
        Uuid::new_v4(),
    );
    let ids: Vec<Uuid> = candidates.iter().map(|_| Uuid::new_v4()).collect();
    let deployments: Vec<Value> = candidates
        .iter()
        .zip(&ids)
        .map(|((_, _, overrides), id)| {
            let mut profile = json!({"endpoint":"anthropic_messages","input_ceiling":80,
                "output_ceiling":10,"tools":true,"streaming":true,"transports":["http"],
                "count_rule":"deployment_ceiling.v1","bound_reference":"fixture"});
            for (k, v) in overrides.as_object().unwrap() {
                profile[k] = v.clone();
            }
            let (family, format) = match profile["endpoint"].as_str().unwrap() {
                "openai_responses" => ("openai_compatible", "openai.responses.v1"),
                "bedrock_converse" => ("aws_bedrock","aws.bedrock.converse.v1"),
                _ => ("anthropic", "anthropic.messages.v1"),
            };
            json!({"id":id,"model_id":model,"upstream_model":format!("actual-{id}"),
                "base_url":"https://upstream.example/v1","region":"us-east-1","credential_id":credential,
                "credential_version":1,"protocol_family":family,"usage_format":format,
                "relay_profile":profile})
        })
        .collect();
    let route: Vec<Value> = candidates
        .iter()
        .zip(&ids)
        .map(|((priority, weight, _), id)| {
            json!({"deployment_id":id,"priority":priority,"weight":weight})
        })
        .collect();
    let mut resources = json!({
        "key_verifiers":[{"id":key,"project_id":project,"organization_id":org,
            "digest":format!("{:x}",Sha256::digest(b"enterprise")),"expires_at":null}],
        "models":[{"id":model,"public_name":"public","token_dimensions":["input_cache_read_tokens","input_cache_write_30m_tokens","input_cache_write_5m_tokens"],"max_input_tokens":100,
            "max_output_tokens":20,"capabilities":{"tools":true,"streaming":true}}],
        "routes":[{"organization_id":org,"model_id":model,"candidates":route}],
        "access_policies":[{"organization_id":org,"scope_id":org,"scope_type":"organization",
            "layer":"platform","allowed_models":[model]}],
        "quota_policies":[],
        "credential_refs":[{"credential_id":credential,"credential_version":1,"key_version":1,
            "algorithm":"libsodium-sealed-box"}],
        "deployments":deployments,
    });
    if candidates
        .iter()
        .any(|(_, _, p)| p.get("web_search_ceiling").is_some())
    {
        resources["models"][0]["tool_dimensions"] = json!(["web_search_requests"]);
    }
    if candidates
        .iter()
        .any(|(_, _, p)| p.get("bedrock_guardrail_unit_ceiling").is_some())
    {
        resources["models"][0]["tool_dimensions"] = json!(["guardrail_content_units"]);
    }
    let mut snapshot = Snapshot {
        applied_revision: 0,
        node_id: Uuid::new_v4(),
        cluster_id: Uuid::new_v4(),
        revision: 7,
        digest: String::new(),
        required_capabilities: [
            "relay.native.v1",
            "relay.bedrock-converse.v1",
            "relay.bedrock-settings.v1",
            "relay.bedrock-guardrail-usage.v1",
            "relay.bedrock-request-options.v1",
            "relay.prompt-cache.v1",
            "relay.web-search.v1",
            "relay.sse.v1",
            "relay.responses-ws.v1",
            "relay.messages-to-responses.v1",
            "relay.responses-to-messages.v1",
            "relay.provider-features.v1",
            "relay.stored-responses.v1",
            "relay.background-responses.v1",
            "relay.conversations.v1",
            "relay.files.v1",
            "relay.remote-mcp.v1",
        ]
        .map(String::from)
        .to_vec(),
        resources,
    };
    Ok(Fixture {
        index: Index::build(&mut snapshot)?,
        ids,
    })
}

fn messages(extra: Value) -> Generate {
    let mut body = json!({"model":"public","max_tokens":6,
        "messages":[{"role":"user","content":"hello"}]});
    for (k, v) in extra.as_object().unwrap() {
        body[k] = v.clone();
    }
    Generate::parse(Endpoint::AnthropicMessages, body, false).unwrap()
}

const FROZEN: ReservationBounds = ReservationBounds {
    guardrail_units: 0,
    searches: 0,
    file_searches: 0,
    interpreter: None,
    execution_mode: nexus_relay::protocol::execution::ExecutionMode::None,
    execution_seconds: 0,
    inputs: 80,
    outputs: 6,
};

fn fallback<'a>(f: &'a Fixture, request: &Generate, failed: &[Uuid]) -> Option<Selection<'a>> {
    f.index.fallback("enterprise", request, failed, FROZEN)
}

#[test]
fn excluded_deployment_leaves_the_rest_of_its_group_by_remaining_weight() {
    let f = fixture(&[
        (0, 5000, json!({})),
        (0, 1, json!({})),
        (0, 1, json!({})),
        (1, 10000, json!({})),
    ]);
    let request = messages(json!({}));
    let mut drawn = HashSet::new();
    for _ in 0..400 {
        let next = fallback(&f, &request, &f.ids[..1]).unwrap();
        assert_ne!(next.deployment.id, f.ids[0]);
        assert_ne!(
            next.deployment.id, f.ids[3],
            "a remaining group member wins"
        );
        assert_eq!(next.bounds, FROZEN);
        assert_eq!(next.body["model"], next.deployment.upstream_model);
        drawn.insert(next.deployment.id);
    }
    assert_eq!(drawn, HashSet::from([f.ids[1], f.ids[2]]));
    let next = fallback(&f, &request, &f.ids[..2]).unwrap();
    assert_eq!(next.deployment.id, f.ids[2]);
}

#[test]
fn exhausted_group_falls_back_to_the_next_priority_and_then_stops() {
    let f = fixture(&[
        (3, 1, json!({})),
        (0, 1, json!({})),
        (1, 1, json!({})),
        (0, 1, json!({})),
    ]);
    let request = messages(json!({}));
    let first = f
        .index
        .select("enterprise", &request)
        .unwrap()
        .deployment
        .id;
    assert!(first == f.ids[1] || first == f.ids[3]);
    let group = [f.ids[1], f.ids[3]];
    assert_eq!(
        fallback(&f, &request, &group).unwrap().deployment.id,
        f.ids[2]
    );
    let lower = [f.ids[1], f.ids[3], f.ids[2]];
    assert_eq!(
        fallback(&f, &request, &lower).unwrap().deployment.id,
        f.ids[0]
    );
    assert!(fallback(&f, &request, &f.ids).is_none());
    assert!(
        f.index
            .fallback("unknown", &request, &f.ids[..1], FROZEN)
            .is_none()
    );
}

#[test]
fn a_fallback_must_fit_the_frozen_reservation() {
    // The first candidate reserved 80 inputs and the client's 6 outputs.
    let f = fixture(&[
        (0, 1, json!({})),
        (1, 1, json!({"input_ceiling":100})),
        (2, 1, json!({"output_ceiling":5})),
        (3, 1, json!({"input_ceiling":40,"output_ceiling":6})),
    ]);
    let request = messages(json!({}));
    let next = fallback(&f, &request, &f.ids[..1]).unwrap();
    assert_eq!(next.deployment.id, f.ids[3]);
    assert_eq!(
        next.bounds,
        ReservationBounds {
            guardrail_units: 0,
            searches: 0,
            file_searches: 0,
            interpreter: None,
            execution_mode: nexus_relay::protocol::execution::ExecutionMode::None,
            execution_seconds: 0,
            inputs: 40,
            outputs: 6
        }
    );
    assert!(fallback(&f, &request, &[f.ids[0], f.ids[3]]).is_none());

    // Responses omits its output limit. The first deployment's ceiling became the frozen
    // output bound; a larger fallback still sends exactly that reserved limit.
    let f = fixture(&[
        (
            0,
            1,
            json!({"endpoint":"openai_responses","output_ceiling":6}),
        ),
        (
            1,
            1,
            json!({"endpoint":"openai_responses","output_ceiling":20}),
        ),
    ]);
    let request = Generate::parse(
        Endpoint::OpenaiResponses,
        json!({"model":"public","input":"hi"}),
        false,
    )
    .unwrap();
    let first = f.index.select("enterprise", &request).unwrap();
    assert_eq!(first.bounds.outputs, 6);
    let next = fallback(&f, &request, &f.ids[..1]).unwrap();
    assert_eq!(next.body["max_output_tokens"], 6);
    assert_eq!(next.bounds.outputs, 6);
    let tighter = ReservationBounds {
        guardrail_units: 0,
        searches: 0,
        file_searches: 0,
        interpreter: None,
        execution_mode: nexus_relay::protocol::execution::ExecutionMode::None,
        execution_seconds: 0,
        inputs: 80,
        outputs: 4,
    };
    let next = f
        .index
        .fallback("enterprise", &request, &f.ids[..1], tighter)
        .unwrap();
    assert_eq!(next.body["max_output_tokens"], 4);
}

#[test]
fn a_fallback_keeps_request_conditions_and_requires_a_working_conversion() {
    let f = fixture(&[
        (0, 1, json!({})),
        (
            1,
            1,
            json!({"transports":["websocket"],"endpoint":"openai_responses"}),
        ),
        (2, 1, json!({"streaming":false})),
        (3, 1, json!({"tools":false})),
        (
            4,
            1,
            json!({"endpoint":"openai_responses","convert_from":"anthropic_messages"}),
        ),
    ]);
    let plain = messages(json!({}));
    // A native Responses deployment without `convert_from` cannot serve Messages.
    assert_eq!(
        fallback(&f, &plain, &f.ids[..1]).unwrap().deployment.id,
        f.ids[2]
    );
    let streamed = messages(json!({"stream":true}));
    assert_eq!(
        fallback(&f, &streamed, &f.ids[..1]).unwrap().deployment.id,
        f.ids[3]
    );
    let tools = messages(
        json!({"stream":true,"tools":[{"name":"lookup","input_schema":{
        "type":"object","properties":{"q":{"type":"string"}},"required":["q"]}}]}),
    );
    let converted = fallback(&f, &tools, &f.ids[..1]).unwrap();
    assert_eq!(converted.deployment.id, f.ids[4]);
    assert_eq!(converted.body["max_output_tokens"], 6);
    assert_eq!(converted.body["model"], converted.deployment.upstream_model);
    // Messages stop sequences cannot be expressed in Responses, so the conversion fails.
    let unportable = messages(
        json!({"stream":true,"tools":[{"name":"lookup","input_schema":{
        "type":"object","properties":{}}}],"stop_sequences":["END"]}),
    );
    assert!(fallback(&f, &unportable, &f.ids[..1]).is_none());

    // HTTP failover never takes a deployment that only declares WebSocket transport.
    let f = fixture(&[
        (0, 1, json!({"endpoint":"openai_responses"})),
        (
            1,
            1,
            json!({"endpoint":"openai_responses","transports":["websocket"]}),
        ),
        (
            2,
            1,
            json!({"endpoint":"openai_responses","transports":["websocket","http"]}),
        ),
    ]);
    let request = Generate::parse(
        Endpoint::OpenaiResponses,
        json!({"model":"public","input":"hi"}),
        false,
    )
    .unwrap();
    assert_eq!(
        fallback(&f, &request, &f.ids[..1]).unwrap().deployment.id,
        f.ids[2]
    );
    assert!(fallback(&f, &request, &[f.ids[0], f.ids[2]]).is_none());
}

#[test]
fn only_rejections_before_execution_are_declined() {
    for status in [429, 503, 529] {
        assert!(declined(StatusCode::from_u16(status).unwrap()), "{status}");
    }
    for status in [200, 400, 401, 404, 408, 409, 413, 500, 502, 504] {
        assert!(!declined(StatusCode::from_u16(status).unwrap()), "{status}");
    }
    assert!(UpstreamError::Connect.unsent());
    assert!(UpstreamError::Signing.unsent());
    // These may follow a request that the upstream already received.
    for error in [
        UpstreamError::Timeout,
        UpstreamError::Transport,
        UpstreamError::Redirect,
        UpstreamError::ResponseTooLarge,
    ] {
        assert!(!error.unsent(), "{error}");
    }
}

#[test]
fn documented_vendor_rejections_are_declined_and_unknown_429s_are_refused() {
    use ErrorContract::{Anthropic, Bedrock, OpenAi};
    let status = |n| StatusCode::from_u16(n).unwrap();
    let anthropic = |kind: &str| {
        serde_json::to_vec(&json!({"type":"error","error":{"type":kind,"message":"x"}})).unwrap()
    };
    let openai = |kind: &str, code: Value| {
        serde_json::to_vec(&json!({"error":{"type":kind,"code":code,"message":"x"}})).unwrap()
    };
    type Case<'a> = (ErrorContract, u16, Option<&'a str>, Vec<u8>, Refusal);
    let cases: Vec<Case> = vec![
        (
            Anthropic,
            429,
            None,
            anthropic("rate_limit_error"),
            Refusal::Declined,
        ),
        (
            Anthropic,
            529,
            None,
            anthropic("overloaded_error"),
            Refusal::Declined,
        ),
        (
            Anthropic,
            503,
            None,
            anthropic("overloaded_error"),
            Refusal::Declined,
        ),
        // A new 429 type still refuses the request; it is not proof enough to resend.
        (
            Anthropic,
            429,
            None,
            anthropic("acceleration_limit_error"),
            Refusal::Refused,
        ),
        (
            Anthropic,
            503,
            None,
            anthropic("gateway_error"),
            Refusal::Unknown,
        ),
        (
            Anthropic,
            529,
            None,
            b"<html>busy</html>".to_vec(),
            Refusal::Unknown,
        ),
        (Anthropic, 429, None, b"too many".to_vec(), Refusal::Unknown),
        (
            OpenAi,
            429,
            None,
            openai("requests", json!("rate_limit_exceeded")),
            Refusal::Declined,
        ),
        (
            OpenAi,
            429,
            None,
            openai("insufficient_quota", json!("insufficient_quota")),
            Refusal::Declined,
        ),
        (
            OpenAi,
            429,
            None,
            openai("rate_limit_error", json!("slow_down")),
            Refusal::Declined,
        ),
        (
            OpenAi,
            429,
            None,
            openai("insufficient_quota", Value::Null),
            Refusal::Declined,
        ),
        (
            OpenAi,
            429,
            None,
            openai("billing", json!("credit_balance_exhausted")),
            Refusal::Declined,
        ),
        (
            OpenAi,
            429,
            None,
            openai("x", json!("organization_spend_limit_exceeded")),
            Refusal::Declined,
        ),
        (
            OpenAi,
            429,
            None,
            openai("x", json!("project_spend_limit_exceeded")),
            Refusal::Declined,
        ),
        (
            OpenAi,
            429,
            None,
            openai("x", json!("organization_usage_limit_exceeded")),
            Refusal::Declined,
        ),
        (
            OpenAi,
            429,
            None,
            openai("tokens", json!("new_limit_kind")),
            Refusal::Refused,
        ),
        (
            OpenAi,
            503,
            None,
            openai("service_unavailable_error", json!("server_is_overloaded")),
            Refusal::Declined,
        ),
        // An OpenAI 503 server_error may follow execution and is never resent.
        (
            OpenAi,
            503,
            None,
            openai("server_error", Value::Null),
            Refusal::Unknown,
        ),
        (
            OpenAi,
            503,
            None,
            openai("server_error", json!("server_is_overloaded")),
            Refusal::Unknown,
        ),
        (
            OpenAi,
            502,
            None,
            openai("rate_limit_error", json!("slow_down")),
            Refusal::Unknown,
        ),
        (
            Bedrock,
            429,
            Some("ThrottlingException"),
            vec![],
            Refusal::Declined,
        ),
        (
            Bedrock,
            429,
            Some("ModelNotReadyException:http://internal.amazon.com/coral/com.amazon.bedrock/"),
            vec![],
            Refusal::Declined,
        ),
        (
            Bedrock,
            503,
            Some("ServiceUnavailableException"),
            vec![],
            Refusal::Declined,
        ),
        (
            Bedrock,
            429,
            Some("SomeNewException"),
            vec![],
            Refusal::Refused,
        ),
        (Bedrock, 429, None, vec![], Refusal::Unknown),
        (
            Bedrock,
            503,
            Some("InternalServerException"),
            vec![],
            Refusal::Unknown,
        ),
        (
            Bedrock,
            502,
            Some("ThrottlingException"),
            vec![],
            Refusal::Unknown,
        ),
    ];
    for (contract, code, header, body, expected) in cases {
        assert_eq!(
            refusal(contract, status(code), header, &body),
            expected,
            "{contract:?} {code} {header:?} {}",
            String::from_utf8_lossy(&body)
        );
    }
}

#[test]
fn only_proof_that_nothing_ran_reports_a_declined_attempt() {
    // The control plane confirms a declined attempt at zero cost without a review.
    assert_eq!(DECLINED, "declined");
    assert_eq!(Refusal::Declined.outcome(), DECLINED);
    // A new 429 type refused the request but is no proof: its missing usage is reviewed.
    assert_eq!(Refusal::Refused.outcome(), "failed");
    // An intermediary's error may follow execution.
    assert_eq!(Refusal::Unknown.outcome(), "unknown");
    let status = |n| StatusCode::from_u16(n).unwrap();
    let overloaded = br#"{"type":"error","error":{"type":"overloaded_error"}}"#;
    let anthropic = |code, body: &[u8]| refusal(ErrorContract::Anthropic, status(code), None, body);
    assert_eq!(anthropic(529, overloaded).outcome(), "declined");
    assert_eq!(anthropic(503, b"<html>busy</html>").outcome(), "unknown");
    let novel = br#"{"type":"error","error":{"type":"acceleration_limit_error"}}"#;
    assert_eq!(anthropic(429, novel).outcome(), "failed");
}

async fn send(port: u16) -> UpstreamError {
    let upstream = Upstream::new(1 << 20, None).unwrap();
    let base = format!("https://127.0.0.1:{port}/v1").parse().unwrap();
    match upstream
        .generate(
            &base,
            Endpoint::AnthropicMessages,
            "secret",
            &json!({"model":"m"}),
        )
        .await
    {
        Ok(_) => panic!("no upstream answered"),
        Err(error) => error,
    }
}

#[tokio::test]
async fn refused_connection_and_failed_tls_handshake_were_never_sent() {
    let closed = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = closed.local_addr().unwrap().port();
    drop(closed);
    let refused = send(port).await;
    assert!(
        matches!(refused, UpstreamError::Connect),
        "refused: {refused}"
    );

    // The peer accepts TCP and closes it before any TLS handshake completes.
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = listener.local_addr().unwrap().port();
    let server = tokio::spawn(async move {
        for _ in 0..4 {
            let Ok((socket, _)) = listener.accept().await else {
                return;
            };
            drop(socket);
        }
    });
    let handshake = send(port).await;
    assert!(handshake.unsent(), "handshake: {handshake}");
    server.abort();
}

#[test]
fn sticky_workflows_keep_prefixes_warm_without_losing_weight_or_fallback() {
    let f = fixture(&[(0, 1, json!({})), (0, 3, json!({})), (1, 100, json!({}))]);
    let mut heavy = 0;
    for i in 0..1024 {
        let mut r = messages(json!({}));
        r.set_affinity_key(Some(format!("workflow-{i}")));
        let dep = f.index.select("enterprise", &r).unwrap().deployment.id;
        heavy += usize::from(dep == f.ids[1]);
        for _ in 0..3 {
            assert_eq!(f.index.select("enterprise", &r).unwrap().deployment.id, dep);
        }
        let backup = fallback(&f, &r, &[dep]).unwrap().deployment.id;
        assert_ne!(dep, backup);
        assert_ne!(backup, f.ids[2]);
        assert_eq!(
            fallback(&f, &r, &[dep, backup]).unwrap().deployment.id,
            f.ids[2]
        );
    }
    assert!(
        (675..850).contains(&heavy),
        "3:1 weighted assignment: {heavy}/1024"
    );
}

#[test]
fn appending_turns_preserves_the_converted_prefix_key_and_deployment() {
    let config = json!({"endpoint":"openai_responses","convert_from":"anthropic_messages","auto_prompt_cache":true});
    let f = fixture(&[(0, 1, config.clone()), (0, 1, config)]);
    let mut first = messages(
        json!({"system":[{"type":"text","text":"rules","cache_control":{"type":"ephemeral"}}],
        "tools":[{"name":"lookup","input_schema":{"type":"object","properties":{}}}]}),
    );
    let first_body = f.index.select("enterprise", &first).unwrap();
    let next = messages(
        json!({"system":[{"type":"text","text":"rules","cache_control":{"type":"ephemeral"}}],
        "tools":[{"name":"lookup","input_schema":{"type":"object","properties":{}}}],
        "messages":[{"role":"user","content":"hello"},{"role":"assistant","content":[{"type":"tool_use","id":"call","name":"lookup","input":{}}]},
        {"role":"user","content":[{"type":"tool_result","tool_use_id":"call","content":"result"}]}]}),
    );
    let continued = f.index.select("enterprise", &next).unwrap();
    assert_eq!(first_body.deployment.id, continued.deployment.id);
    assert_eq!(
        first_body.body["prompt_cache_key"],
        continued.body["prompt_cache_key"]
    );
    assert_eq!(first_body.body["tools"], continued.body["tools"]);
    assert_eq!(
        &continued.body["input"].as_array().unwrap()[..2],
        first_body.body["input"].as_array().unwrap().as_slice()
    );
    assert_eq!(
        continued.body["input"][0]["content"][0]["prompt_cache_breakpoint"]["mode"],
        "explicit"
    );
    assert_eq!(continued.body["input"][2]["call_id"], "call");
    assert_eq!(continued.body["input"][3]["call_id"], "call");
    first.set_affinity_key(Some("unlogged-workflow".into()));
    let changed = f.index.select("enterprise", &first).unwrap();
    assert_ne!(
        changed.body["prompt_cache_key"],
        first_body.body["prompt_cache_key"]
    );
    assert_ne!(changed.body["prompt_cache_key"], "unlogged-workflow");
}

#[test]
fn replaying_the_same_workload_improves_deployment_local_prefix_reuse() {
    use std::collections::HashMap;
    let baseline = fixture(&[
        (0, 1, json!({})),
        (0, 1, json!({})),
        (0, 1, json!({})),
        (0, 1, json!({})),
    ]);
    let optimized = fixture(&[
        (0, 1, json!({"auto_prompt_cache":true})),
        (0, 1, json!({"auto_prompt_cache":true})),
        (0, 1, json!({"auto_prompt_cache":true})),
        (0, 1, json!({"auto_prompt_cache":true})),
    ]);
    fn replay(index: &Index) -> (u64, u64) {
        let mut prefixes = HashMap::new();
        let (mut read, mut input) = (0, 0);
        for tenant_workflow in 0..96 {
            let mut history = vec![
                json!({"role":"user","content":format!("reference document {tenant_workflow}")}),
            ];
            for turn in 0..12 {
                // Fixed synthetic token counts: 2048 reusable tokens followed by 64 new tokens per turn.
                let tokens = 2048 + turn * 64;
                let req = messages(json!({"messages":history}));
                let selected = index.select("enterprise", &req).unwrap();
                let slot = prefixes
                    .entry((tenant_workflow, selected.deployment.id))
                    .or_insert(0);
                read += *slot;
                input += tokens;
                *slot = tokens;
                history.extend([
                    json!({"role":"assistant","content":"answer"}),
                    json!({"role":"user","content":format!("step {turn}")}),
                ]);
            }
        }
        (read, input)
    }
    let (base, total) = replay(&baseline.index);
    let (sticky, input) = replay(&optimized.index);
    assert_eq!(total, input);
    assert_eq!(sticky, 96 * (0..11).map(|i| 2048 + i * 64).sum::<u64>());
    assert!(sticky > base + total / 10);
    println!(
        "deployment-local synthetic prefix reads: random={base}, affinity={sticky}, total={total}"
    );
}

#[test]
fn native_and_converted_workflow_use_the_same_scoped_responses_key() {
    let config = json!({"endpoint":"openai_responses","convert_from":"anthropic_messages","auto_prompt_cache":true});
    let f = fixture(&[(0, 1, config)]);
    let native = Generate::parse(
        Endpoint::OpenaiResponses,
        json!({"model":"public","input":"hello","prompt_cache_key":"session"}),
        false,
    )
    .unwrap();
    let mut cross = messages(json!({}));
    cross.set_affinity_key(Some("session".to_owned()));
    let a = f.index.select("enterprise", &native).unwrap();
    let b = f.index.select("enterprise", &cross).unwrap();
    assert_eq!(a.body["prompt_cache_key"], b.body["prompt_cache_key"]);
    assert_eq!(a.body["prompt_cache_key"].as_str().unwrap().len(), 64);
}

#[test]
fn compact_routes_only_to_native_http_responses_within_its_frozen_bounds() {
    let responses = |limit| json!({"endpoint":"openai_responses","output_ceiling":limit});
    let cross = json!({"convert_from":"openai_responses"});
    let req = Generate::parse_compact(json!({"model":"public","input":"context"})).unwrap();
    let config = fixture(&[
        (0, 1, cross.clone()),
        (1, 1, responses(6)),
        (2, 1, responses(8)),
        (3, 1, responses(4)),
    ]);
    let first = config.index.select("enterprise", &req).unwrap();
    assert_eq!(first.deployment.id, config.ids[1]);
    assert_eq!(first.bounds.outputs, 6);
    let second = config
        .index
        .fallback("enterprise", &req, &[first.deployment.id], first.bounds)
        .unwrap();
    assert_eq!(second.deployment.id, config.ids[3]);
    assert_eq!(second.bounds.outputs, 4);
    assert!(second.body.get("max_output_tokens").is_none());
    assert!(
        fixture(&[(0, 1, cross)])
            .index
            .select("enterprise", &req)
            .is_err()
    );
}

#[test]
fn provider_features_are_off_until_the_deployment_enables_them() {
    let responses = |features: Value| {
        json!({"endpoint":"openai_responses","count_rule":"o200k_base.v1",
            "provider_features":features})
    };
    let generate = |body: Value| {
        let mut base = json!({"model":"public","input":"hi"});
        for (k, v) in body.as_object().unwrap() {
            base[k] = v.clone();
        }
        Generate::parse(Endpoint::OpenaiResponses, base, false).unwrap()
    };
    let mcp = json!({"tools":[{"type":"mcp","server_label":"docs",
        "server_url":"https://example.org/mcp","require_approval":"never"}]});
    let off = fixture(&[(0, 1, responses(json!([])))]);
    for body in [
        json!({"store":true}),
        json!({"store":true,"background":true}),
        json!({"conversation":format!("conv_nx_{}", Uuid::new_v4())}),
        mcp.clone(),
    ] {
        let rejected = off.index.select("enterprise", &generate(body)).err();
        assert!(matches!(
            rejected,
            Some(nexus_relay::control::snapshot::Rejection::FeatureDisabled)
        ));
    }
    assert!(off.index.select("enterprise", &generate(json!({}))).is_ok());
    assert!(
        off.index
            .select("enterprise", &generate(json!({"store":false})))
            .is_ok()
    );
    let on = fixture(&[(0, 1, responses(json!(["stored_responses", "remote_mcp"])))]);
    assert!(
        on.index
            .select("enterprise", &generate(json!({"store":true})))
            .is_ok()
    );
    assert!(on.index.select("enterprise", &generate(mcp)).is_ok());
    assert!(
        on.index
            .select(
                "enterprise",
                &generate(json!({"store":true,"background":true}))
            )
            .is_err()
    );
    // Features must suit the endpoint, require their node capability and be listed once.
    for profile in [
        responses(json!(["background"])),
        responses(json!(["batches"])),
        responses(json!(["vector_stores"])),
        responses(json!(["files", "files"])),
        json!({"endpoint":"anthropic_messages","provider_features":["stored_responses"]}),
        json!({"endpoint":"bedrock_converse","provider_features":["files"]}),
    ] {
        assert!(build(&[(0, 1, profile)]).is_err());
    }
}

#[test]
fn search_failover_preserves_the_frozen_call_limit() {
    let search = |n| json!({"web_search_ceiling":n,"provider_features":["web_search"]});
    let f = fixture(&[(0, 1, search(2)), (1, 1, search(1)), (2, 1, search(4))]);
    let req = messages(json!({"tools":[{"type":"web_search_20250305","name":"web_search"}]}));
    let first = f.index.select("enterprise", &req).unwrap();
    assert_eq!(first.bounds.searches, 2);
    let second = f
        .index
        .fallback("enterprise", &req, &f.ids[..1], first.bounds)
        .unwrap();
    assert_eq!(second.deployment.id, f.ids[2]);
    assert_eq!(second.body["tools"][0]["max_uses"], 2);
    assert_eq!(second.bounds, first.bounds);
}

#[test]
fn looping_requests_reserve_input_for_each_model_call() {
    let f = fixture(&[(
        0,
        1,
        json!({"web_search_ceiling":2,"provider_features":["web_search"]}),
    )]);
    let plain = f.index.select("enterprise", &messages(json!({}))).unwrap();
    assert_eq!(plain.bounds.inputs, 80);
    let search = messages(json!({"tools":[{"type":"web_search_20250305","name":"web_search"}]}));
    let looped = f.index.select("enterprise", &search).unwrap();
    // The prompt is read again after each of the two permitted searches.
    assert_eq!((looped.bounds.searches, looped.bounds.inputs), (2, 240));
    assert_eq!(looped.bounds.outputs, 6);
}

#[test]
fn guardrail_profiles_require_a_unit_bound_in_both_directions() {
    let guardrail = json!({"guardrailIdentifier":"guard","guardrailVersion":"1"});
    for profile in [
        json!({"endpoint":"bedrock_converse","bedrock_guardrail":guardrail}),
        json!({"endpoint":"bedrock_converse","bedrock_guardrail":guardrail,
            "bedrock_guardrail_unit_ceiling":0}),
        json!({"endpoint":"bedrock_converse",
            "bedrock_request_options":[{"guardrailConfig":guardrail}]}),
        json!({"endpoint":"bedrock_converse","bedrock_guardrail_unit_ceiling":4}),
    ] {
        assert!(build(&[(0, 1, profile)]).is_err());
    }
    assert!(
        build(&[(
            0,
            1,
            json!({"endpoint":"bedrock_converse","bedrock_guardrail":guardrail,
                "bedrock_guardrail_unit_ceiling":4})
        )])
        .is_ok()
    );
}

#[test]
fn guardrail_backup_cannot_raise_the_frozen_policy_exposure() {
    let profile = |units| {
        json!({"endpoint":"bedrock_converse","bedrock_guardrail_unit_ceiling":units,
            "bedrock_guardrail":{"guardrailIdentifier":"guard","guardrailVersion":"1","trace":"enabled"}})
    };
    let f = fixture(&[
        (0, 1, profile(4)),
        (1, 1, profile(6)),
        (2, 1, json!({})),
        (3, 1, profile(2)),
    ]);
    let req = messages(json!({}));
    let first = f.index.select("enterprise", &req).unwrap();
    assert_eq!(first.bounds.guardrail_units, 4);
    let backup = f
        .index
        .fallback("enterprise", &req, &[first.deployment.id], first.bounds)
        .unwrap();
    assert_eq!(backup.deployment.id, f.ids[3]);
    assert_eq!(backup.bounds.guardrail_units, 4);
    assert!(
        f.index
            .fallback("enterprise", &req, &[f.ids[0], f.ids[3]], first.bounds)
            .is_none()
    );
}

#[test]
fn exact_route_wins_over_preferred_approximation_and_retries_need_consent() {
    use nexus_relay::protocol::approximation::Choices;
    let f = fixture(&[
        (
            0,
            1,
            json!({"endpoint":"openai_responses","convert_from":"anthropic_messages","auto_prompt_cache":true}),
        ),
        (9, 1, json!({"endpoint":"anthropic_messages"})),
    ]);
    let mut req = messages(
        json!({"stop_sequences":["END"],"messages":[{"role":"user","content":[{"type":"text","text":"go","cache_control":{"type":"ephemeral","ttl":"5m"}}]},{"role":"assistant","content":"Once"}]}),
    );
    let exact = f.index.select("enterprise", &req).unwrap();
    assert_eq!(exact.deployment.id, f.ids[1]);
    assert!(
        f.index
            .fallback("enterprise", &req, &[exact.deployment.id], exact.bounds)
            .is_none()
    );
    let mut h = http::HeaderMap::new();
    h.insert(
        "x-nexus-allow-approximation",
        "cache-5m=30m, stop=local, prefill=prompt".parse().unwrap(),
    );
    req.allow_approximation = Choices::parse(&h).unwrap();
    let exact = f.index.select("enterprise", &req).unwrap();
    assert_eq!(exact.deployment.id, f.ids[1]);
    assert_eq!(exact.body["stop_sequences"], json!(["END"]));
    assert_eq!(exact.body["messages"][1]["content"], "Once");
    let backup = f
        .index
        .fallback("enterprise", &req, &[exact.deployment.id], exact.bounds)
        .unwrap();
    assert_eq!(backup.deployment.id, f.ids[0]);
    assert_eq!(
        backup.body["input"][0]["content"][0]["prompt_cache_breakpoint"]["ttl"],
        "30m"
    );
    assert!(backup.body.get("stop_sequences").is_none());
    assert_eq!(backup.body["input"][2]["role"], "user");
}

#[test]
fn reverse_explicit_cache_lifetime_is_inherited_by_breakpoints_and_the_automatic_marker() {
    use nexus_relay::protocol::approximation::Choices;
    let mut h = http::HeaderMap::new();
    h.insert(
        "x-nexus-allow-approximation",
        "cache-30m=5m, cache-12h=5m".parse().unwrap(),
    );
    let choice = Choices::parse(&h).unwrap();
    for ep in ["anthropic_messages", "bedrock_converse"] {
        let mut profile = json!({"endpoint":ep,"auto_prompt_cache":true});
        if ep == "anthropic_messages" {
            profile["convert_from"] = "openai_responses".into();
        }
        let f = fixture(&[(0, 1, profile)]);
        let mut req=Generate::parse(Endpoint::OpenaiResponses,json!({"model":"public","input":[{"role":"user","content":[
                {"type":"input_text","text":"a","prompt_cache_breakpoint":{"mode":"explicit"}},
                {"type":"input_text","text":"b","prompt_cache_breakpoint":{"mode":"explicit","ttl":"12h"}},
                {"type":"input_text","text":"c"}]}],"prompt_cache_options":{"mode":"implicit","ttl":"30m"}}),false).unwrap();
        assert!(f.index.select("enterprise", &req).is_err());
        req.allow_approximation = choice;
        let target = f.index.select("enterprise", &req).unwrap().body;
        if ep == "anthropic_messages" {
            assert_eq!(target["cache_control"]["ttl"], "5m");
            assert_eq!(
                target["messages"][0]["content"][0]["cache_control"]["ttl"],
                "5m"
            );
            assert_eq!(
                target["messages"][0]["content"][1]["cache_control"]["ttl"],
                "5m"
            );
            assert!(
                target["messages"][0]["content"][2]
                    .get("cache_control")
                    .is_none()
            );
        } else {
            let markers = target["messages"][0]["content"]
                .as_array()
                .unwrap()
                .iter()
                .filter_map(|p| p.get("cachePoint"))
                .collect::<Vec<_>>();
            assert_eq!(markers.len(), 3);
            assert!(markers.iter().all(|p| p["ttl"] == "5m"));
        }
    }
}
