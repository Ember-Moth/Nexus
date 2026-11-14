use http::{HeaderMap, Method};
use nexus_relay::protocol::{
    Endpoint, ProtocolError,
    request::{Generate, Route, route},
    sse::Decoder,
    stream::{Observer, Terminal},
    tokens::{Encoding, ReservationBounds, TextEstimate},
    usage::UsageEvidence,
    websocket::{ClientEvent, parse_event, validate_upgrade},
};
use serde_json::{Value, json};

const MESSAGES: &str = include_str!("fixtures/v1/messages.json");
const RESPONSES: &str = include_str!("fixtures/v1/responses.json");

#[test]
fn wire_routes_credentials_and_forwarded_limits() {
    assert_eq!(route(&Method::GET, "/v1/messages"), None);
    assert_eq!(route(&Method::POST, "/v1/chat/completions"), None);
    assert_eq!(
        route(&Method::GET, "/v1/responses"),
        Some(Route::ResponsesWebSocket)
    );
    for (endpoint, path, raw) in [
        (Endpoint::AnthropicMessages, "/v1/messages", MESSAGES),
        (Endpoint::OpenaiResponses, "/v1/responses", RESPONSES),
    ] {
        assert_eq!(route(&Method::POST, path), Some(Route::Generate(endpoint)));
        let fixture: Value = serde_json::from_str(raw).unwrap();
        let request = Generate::parse(endpoint, fixture["request"].clone(), false).unwrap();
        let (body, bounds) = request.prepare_native("deployed-1", 80, 100).unwrap();
        assert_eq!(body["model"], "deployed-1");
        assert_eq!(bounds.inputs, 80);
        assert!(request.prepare_native("deployed-1", 80, 1).is_err());
        if endpoint == Endpoint::OpenaiResponses {
            assert_eq!(body["store"], false);
        }
    }
    let mut h = HeaderMap::new();
    h.insert("authorization", "Bearer public-key".parse().unwrap());
    assert_eq!(Route::ResponsesWebSocket.key(&h), Ok("public-key"));
    assert!(
        Route::Generate(Endpoint::AnthropicMessages)
            .key(&h)
            .is_err()
    );
    h.append("authorization", "Bearer second".parse().unwrap());
    assert!(Route::ResponsesWebSocket.key(&h).is_err());
}

#[test]
fn stream_fixtures_agree_at_every_chunk_size() {
    for (endpoint, raw) in [
        (Endpoint::AnthropicMessages, MESSAGES),
        (Endpoint::OpenaiResponses, RESPONSES),
    ] {
        let fixture: Value = serde_json::from_str(raw).unwrap();
        let wire = fixture["events"]
            .as_array()
            .unwrap()
            .iter()
            .map(|event| {
                format!(
                    "event: {}\r\ndata: {}\r\n\r\n",
                    event["type"].as_str().unwrap(),
                    event
                )
            })
            .collect::<String>();
        for size in [1, 2, 3, 5, 11, 31, 1024, wire.len()] {
            let mut decoder = Decoder::new(2048);
            let mut observer = Observer::new(endpoint);
            let mut terminal = None;
            for chunk in wire.as_bytes().chunks(size) {
                decoder
                    .feed(chunk, |ev| {
                        let event = serde_json::from_str(&ev.data).unwrap();
                        if let Some(done) = observer.observe(ev.name.as_deref(), &event)? {
                            assert!(terminal.replace(done).is_none());
                        }
                        Ok(())
                    })
                    .unwrap();
            }
            decoder.finish().unwrap();
            observer.finish().unwrap();
            let done = terminal.unwrap();
            assert_eq!(done.terminal, Terminal::Completed);
            assert_eq!(
                serde_json::to_value(done.usage).unwrap(),
                fixture["evidence"]
            );
        }
    }
}

#[test]
fn incomplete_eof_limit_invalid_utf8_and_multiline() {
    let mut d = Decoder::new(64);
    d.feed(b":heartbeat\r\rdata: a\ndata: b\n\n", |e| {
        assert_eq!(e.data, "a\nb");
        Ok(())
    })
    .unwrap();
    d.feed(b"data: partial", |_| panic!()).unwrap();
    assert_eq!(d.finish(), Err(ProtocolError::Truncated));
    assert_eq!(
        Decoder::new(5).feed(b"abcdef", |_| panic!()),
        Err(ProtocolError::EventTooLarge)
    );
    assert!(
        Decoder::new(20)
            .feed(b"data: \xff\n", |_| panic!())
            .is_err()
    );
    let mut observer = Observer::new(Endpoint::OpenaiResponses);
    assert_eq!(observer.finish(), Err(ProtocolError::Truncated));
    assert_eq!(
        observer
            .observe(
                None,
                &json!({"type":"response.failed","response":{"id":"r","status":"failed"}})
            )
            .unwrap()
            .unwrap()
            .terminal,
        Terminal::Failed
    );
    assert!(
        observer
            .observe(
                None,
                &json!({"type":"response.completed","response":{"id":"r","status":"completed"}})
            )
            .is_err()
    );
}

#[test]
fn usage_preserves_missing_zero_and_rejects_unmapped_counts() {
    let endpoint = Endpoint::OpenaiResponses;
    assert!(
        UsageEvidence::extract(endpoint, None)
            .unwrap()
            .raw_usage
            .is_none()
    );
    let evidence = UsageEvidence::extract(endpoint, Some(&json!({"input_tokens":0}))).unwrap();
    let raw = evidence.raw_usage.unwrap();
    assert_eq!(raw.get("input_tokens"), Some(&0));
    assert!(!raw.contains_key("output_tokens"));
    for v in [
        json!({"input_tokens":true}),
        json!({"output_tokens":1.5}),
        json!({"tool_cost":3}),
        json!({"input_tokens":1_000_000_001}),
    ] {
        assert!(UsageEvidence::extract(endpoint, Some(&v)).is_err());
    }
}

#[test]
fn websocket_envelopes_and_handshake() {
    let mut h = HeaderMap::new();
    h.insert("connection", "keep-alive, Upgrade".parse().unwrap());
    h.insert("upgrade", "websocket".parse().unwrap());
    h.insert("sec-websocket-version", "13".parse().unwrap());
    assert!(validate_upgrade(&Method::GET, &h).is_err());
    h.insert(
        "sec-websocket-key",
        "MDEyMzQ1Njc4OWFiY2RlZg==".parse().unwrap(),
    );
    validate_upgrade(&Method::GET, &h).unwrap();
    assert!(validate_upgrade(&Method::POST, &h).is_err());
    let b=br#"{"type":"response.create","model":"m","input":"hi","stream_id":"one","previous_response_id":"parent"}"#;
    let ClientEvent::Create {
        request, stream_id, ..
    } = parse_event(b, 1024).unwrap()
    else {
        panic!()
    };
    assert_eq!(request.previous_response_id(), Some("parent"));
    assert_eq!(stream_id.as_deref(), Some("one"));
    let (body, bounds) = request.prepare_native("real", 200, 50).unwrap();
    assert_eq!(bounds.outputs, 50);
    assert!(body.get("type").is_none() && body.get("stream_id").is_none());
    assert!(parse_event(b, 5).is_err());
    assert!(matches!(
        parse_event(br#"{"type":"response.cancel","response_id":"r"}"#, 256),
        Ok(ClientEvent::Cancel { .. })
    ));
    for b in [
        r#"{"type":"response.create","model":"m","input":[],"store":true}"#,
        r#"{"type":"response.create","model":"m","input":[],"generate":false}"#,
        r#"{"type":"response.steer"}"#,
    ] {
        assert!(parse_event(b.as_bytes(), 512).is_err());
    }
}

#[test]
fn text_estimate_is_independent_of_reservation() {
    for codec in [Encoding::O200kBase, Encoding::Cl100kBase] {
        assert_eq!(codec.estimate("hello world", 32), Ok(TextEstimate(2)));
        assert!(codec.estimate("你好", 4).is_err());
    }
    let bounds = ReservationBounds::for_deployment(1000, 100, Some(10)).unwrap();
    assert_eq!(
        bounds,
        ReservationBounds {
            inputs: 1000,
            outputs: 10
        }
    );
}

#[test]
fn last_messages_delta_cannot_inherit_an_unconfirmed_output_count() {
    let fixture: Value = serde_json::from_str(MESSAGES).unwrap();
    let mut stream = Observer::new(Endpoint::AnthropicMessages);
    stream.observe(None, &fixture["events"][0]).unwrap();
    stream
        .observe(
            None,
            &json!({"type":"message_delta","usage":{"output_tokens":2}}),
        )
        .unwrap();
    stream
        .observe(
            None,
            &json!({"type":"message_delta","delta":{"stop_reason":"end_turn"}}),
        )
        .unwrap();
    let done = stream
        .observe(None, &json!({"type":"message_stop"}))
        .unwrap()
        .unwrap();
    let raw = done.usage.raw_usage.unwrap();
    assert_eq!(raw.get("input_tokens"), Some(&80));
    assert!(!raw.contains_key("output_tokens"));
}

#[test]
fn hosted_tools_are_rejected_and_function_arguments_remain_exact() {
    let raw = r#"{"model":"a","input":[{"type":"function_call","call_id":"f","arguments":"{}","name":"lookup"},{"type":"function_call_output","call_id":"f","output":"42"}],"tools":[{"type":"function","name":"lookup","parameters":{"type":"object","properties":{"x":{"const":12345678901234567890123456789}}}}]}"#;
    let body: Value = serde_json::from_str(raw).unwrap();
    let request = Generate::parse(Endpoint::OpenaiResponses, body.clone(), false).unwrap();
    let prepared = request.prepare_native("upstream", 128, 64).unwrap().0;
    assert_eq!(prepared["input"], body["input"]);
    assert!(
        prepared["tools"]
            .to_string()
            .contains("12345678901234567890123456789")
    );
    let hosted = json!({"model":"a","input":"test","tools":[{"type":"web_search"}]});
    assert!(Generate::parse(Endpoint::OpenaiResponses, hosted, false).is_err());
}

#[test]
fn native_json_responses_agree_with_stream_evidence() {
    use nexus_relay::protocol::response;
    for (endpoint, raw) in [
        (Endpoint::AnthropicMessages, MESSAGES),
        (Endpoint::OpenaiResponses, RESPONSES),
    ] {
        let fixture: Value = serde_json::from_str(raw).unwrap();
        let success = response::observe(endpoint, &fixture["response"]).unwrap();
        assert_eq!(success.terminal, Terminal::Completed);
        assert_eq!(
            serde_json::to_value(success.usage).unwrap(),
            fixture["evidence"]
        );
        let failed = response::observe(endpoint, &fixture["error"]).unwrap();
        assert_eq!(failed.terminal, Terminal::Failed);
        assert!(failed.usage.raw_usage.is_none());
    }
    let incomplete = json!({"object":"response","id":"r","status":"incomplete",
        "incomplete_details":{"reason":"max_output_tokens"}});
    let observed = response::observe(Endpoint::OpenaiResponses, &incomplete).unwrap();
    assert_eq!(observed.terminal, Terminal::Incomplete);
    assert_eq!(observed.stop_reason.as_deref(), Some("max_output_tokens"));
    assert!(observed.usage.raw_usage.is_none());
}

#[test]
fn websocket_turns_have_separate_native_evidence_and_explicit_parent() {
    let sample: Value = serde_json::from_str(include_str!("fixtures/v1/websocket.json")).unwrap();
    let handshake = &sample["handshake"];
    let method = Method::from_bytes(handshake["method"].as_str().unwrap().as_bytes()).unwrap();
    assert_eq!(
        route(&method, handshake["path"].as_str().unwrap()),
        Some(Route::ResponsesWebSocket)
    );
    let mut headers = HeaderMap::new();
    for (name, value) in handshake["headers"].as_object().unwrap() {
        headers.insert(
            http::HeaderName::from_bytes(name.as_bytes()).unwrap(),
            value.as_str().unwrap().parse().unwrap(),
        );
    }
    validate_upgrade(&method, &headers).unwrap();
    let mut parent = None;
    let mut counts = Vec::new();
    for turn in sample["turns"].as_array().unwrap() {
        let create = serde_json::to_vec(&turn["create"]).unwrap();
        let ClientEvent::Create { request, .. } = parse_event(&create, 2048).unwrap() else {
            panic!()
        };
        assert_eq!(request.previous_response_id(), parent.as_deref());
        let mut observer = Observer::new(Endpoint::OpenaiResponses);
        for event in turn["events"].as_array().unwrap() {
            if let Some(completion) = observer.observe(None, event).unwrap() {
                parent = completion.response_id;
                counts.push(completion.usage.raw_usage.unwrap()["total_tokens"]);
            }
        }
        observer.finish().unwrap();
    }
    assert_eq!(counts, [13, 19]);
    let cancel = serde_json::to_vec(&sample["cancel"]).unwrap();
    let ClientEvent::Cancel { response_id, .. } = parse_event(&cancel, 2048).unwrap() else {
        panic!()
    };
    assert_eq!(response_id, parent);
}
