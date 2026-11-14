//! Failover candidate selection and the classification of provably unexecuted attempts.
use std::collections::HashSet;

use http::StatusCode;
use nexus_relay::{
    control::{
        database::Snapshot,
        snapshot::{Index, Selection},
    },
    protocol::{Endpoint, request::Generate, tokens::ReservationBounds},
    upstream::{Upstream, UpstreamError, declined},
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
                _ => ("anthropic", "anthropic.messages.v1"),
            };
            json!({"id":id,"model_id":model,"upstream_model":format!("actual-{id}"),
                "base_url":"https://upstream.example/v1","credential_id":credential,
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
    let resources = json!({
        "key_verifiers":[{"id":key,"project_id":project,"organization_id":org,
            "digest":format!("{:x}",Sha256::digest(b"enterprise")),"expires_at":null}],
        "models":[{"id":model,"public_name":"public","max_input_tokens":100,
            "max_output_tokens":20,"capabilities":{"tools":true,"streaming":true}}],
        "routes":[{"organization_id":org,"model_id":model,"candidates":route}],
        "access_policies":[{"organization_id":org,"scope_id":org,"scope_type":"organization",
            "layer":"platform","allowed_models":[model]}],
        "quota_policies":[],
        "credential_refs":[{"credential_id":credential,"credential_version":1,"key_version":1,
            "algorithm":"libsodium-sealed-box"}],
        "deployments":deployments,
    });
    let mut snapshot = Snapshot {
        applied_revision: 0,
        node_id: Uuid::new_v4(),
        cluster_id: Uuid::new_v4(),
        revision: 7,
        digest: String::new(),
        required_capabilities: [
            "relay.native.v1",
            "relay.sse.v1",
            "relay.responses-ws.v1",
            "relay.messages-to-responses.v1",
        ]
        .map(String::from)
        .to_vec(),
        resources,
    };
    Fixture {
        index: Index::build(&mut snapshot).unwrap(),
        ids,
    }
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
    for status in [429, 502, 503, 529] {
        assert!(declined(StatusCode::from_u16(status).unwrap()), "{status}");
    }
    for status in [200, 400, 401, 404, 408, 409, 413, 500, 504] {
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
