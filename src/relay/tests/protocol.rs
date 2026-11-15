use http::{HeaderMap, Method};
use nexus_relay::protocol::{
    Endpoint, ProtocolError,
    request::{Generate, Route, route},
    sse::Decoder,
    stream::{Observer, Terminal},
    tokens::{
        CallLoop, DEFAULT_TOOL_CALLS, Encoding, ReservationBounds, SERVER_LOOP_LIMIT, TextEstimate,
        model_calls,
    },
    usage::UsageEvidence,
    websocket::{ClientEvent, parse_event, validate_upgrade},
};
use serde_json::{Value, json};

const MESSAGES: &str = include_str!("fixtures/v1/messages.json");
const RESPONSES: &str = include_str!("fixtures/v1/responses.json");

#[test]
fn wire_routes_credentials_and_forwarded_limits() {
    assert_eq!(route(&Method::GET, "/v1/messages"), None);
    assert_eq!(
        route(&Method::POST, "/v1/chat/completions"),
        Some(Route::Chat)
    );
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
    assert!(
        parse_event(
            br#"{"type":"response.create","model":"m","input":[],"store":true}"#,
            512
        )
        .is_ok()
    );
    for b in [
        r#"{"type":"response.create","model":"m","input":[],"background":true}"#,
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
            guardrail_units: 0,
            searches: 0,
            file_searches: 0,
            interpreter: None,
            execution_mode: nexus_relay::protocol::execution::ExecutionMode::None,
            execution_seconds: 0,
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
fn unsupported_tools_are_rejected_and_function_arguments_remain_exact() {
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
    let hosted = json!({"model":"a","input":"test","tools":[{"type":"unrecognized_search"}]});
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

#[test]
fn responses_usage_item_attribution_and_cache_write_breakdown_do_not_double_count() {
    let endpoint = Endpoint::OpenaiResponses;
    let mut usage = json!({"input_tokens":10,"output_tokens":5,"total_tokens":15,
        "input_tokens_details":{"cached_tokens":0,"cache_write_tokens":0,"cache_write_tokens_details":{"ttl_30m_tokens":0,"ttl_12h_tokens":0}},
        "output_tokens_details":{"reasoning_tokens":0},
        "attribution":{"items":{"test_item":{"input_tokens":10,"output_tokens":5,"cached_tokens":0,"cache_write_tokens":0,"content":[{"input_tokens":10,"output_tokens":5}]}}}});
    let counts = UsageEvidence::extract(endpoint, Some(&usage))
        .unwrap()
        .raw_usage
        .unwrap();
    assert_eq!(
        (
            counts["input_tokens"],
            counts["output_tokens"],
            counts["cache_write_tokens"]
        ),
        (10, 5, 0)
    );
    usage["attribution"]["items"]["test_item"]["output_tokens"] = 6.into();
    assert!(UsageEvidence::extract(endpoint, Some(&usage)).is_err());
    usage["attribution"] = Value::Null;
    usage["input_tokens_details"]["cache_write_tokens_details"]["ttl_12h_tokens"] = 1.into();
    assert!(UsageEvidence::extract(endpoint, Some(&usage)).is_err());
}

#[test]
fn interleaved_tool_arguments_share_a_bounded_budget_until_done() {
    use nexus_relay::protocol::tools::ToolLimits;
    let mut limits = ToolLimits::new(Endpoint::OpenaiResponses, 8);
    let delta = |id: &str, part: &str| json!({"type":"response.function_call_arguments.delta","item_id":id,"delta":part});
    limits.observe(&delta("one", "1234")).unwrap();
    limits.observe(&delta("two", "1234")).unwrap();
    assert!(limits.observe(&delta("one", "x")).is_err());
    limits
        .observe(&json!({"type":"response.function_call_arguments.done","item_id":"two"}))
        .unwrap();
    limits.observe(&delta("one", "x")).unwrap();
}

#[test]
fn compact_wire_uses_explicit_history_and_only_compact_parameters() {
    let path = "/v1/responses/compact";
    assert_eq!(route(&Method::POST, path), Some(Route::Compact));
    assert_eq!(route(&Method::GET, path), None);
    let mut headers = HeaderMap::new();
    headers.insert("authorization", "Bearer tenant".parse().unwrap());
    assert_eq!(Route::Compact.key(&headers), Ok("tenant"));

    let input = json!([{"type":"compaction","id":"cmp_prior","encrypted_content":"opaque"},
                     {"type":"function_call_output","call_id":"call","output":"ok"}]);
    let body = json!({"model":"alias","instructions":"retain constraints","input":input,
                    "prompt_cache_key":"workflow","image":{"context_compression":"enabled"}});
    let req = Generate::parse_compact(body.clone()).unwrap();
    assert!(!req.has_tools() && !req.stream);
    let (wire, bounds) = req.prepare_native("upstream", 400, 80).unwrap();
    let mut expected = body.clone();
    expected["model"] = json!("upstream");
    assert_eq!(wire, expected);
    assert_eq!(
        bounds,
        ReservationBounds {
            guardrail_units: 0,
            searches: 0,
            file_searches: 0,
            interpreter: None,
            execution_mode: nexus_relay::protocol::execution::ExecutionMode::None,
            execution_seconds: 0,
            inputs: 400,
            outputs: 80
        }
    );
    for extra in [
        json!({"stream":true}),
        json!({"max_output_tokens":4}),
        json!({"store":true}),
        json!({"previous_response_id":"resp_foreign"}),
        json!({"input":[{"id":"msg_foreign"}]}),
        json!({"input":[{"type":"item_reference","id":"msg_foreign"}]}),
    ] {
        let mut invalid = body.clone();
        invalid
            .as_object_mut()
            .unwrap()
            .extend(extra.as_object().unwrap().clone());
        assert!(Generate::parse_compact(invalid).is_err());
    }
}

#[test]
fn compatible_gateway_usage_reconciles_duplicate_fields_and_partial_starts() {
    let canonical = json!({"prompt_tokens":33,"completion_tokens":5,"total_tokens":38,
        "prompt_tokens_details":{"cached_tokens":0,"text_tokens":0,"audio_tokens":0,"image_tokens":0},
        "completion_tokens_details":{"reasoning_tokens":3,"audio_tokens":0,"image_tokens":0},
        "input_tokens":0,"output_tokens":0,"input_tokens_details":null,
        "claude_cache_creation_5_m_tokens":0,"claude_cache_creation_1_h_tokens":0,"cost":0});
    let raw = json!({"input_tokens":33,"output_tokens":5,"total_tokens":38,"input_tokens_details":null,
        "prompt_tokens":33,"completion_tokens":5,"billing_usage":{"source":"gateway","semantic":"openai","openai_usage":canonical}});
    let e = UsageEvidence::extract(Endpoint::OpenaiResponses, Some(&raw))
        .unwrap()
        .raw_usage
        .unwrap();
    assert_eq!(e["input_tokens"], 33);
    assert_eq!(e["cached_input_tokens"], 0);
    assert_eq!(e["reasoning_tokens"], 3);
    let mut bad = raw.clone();
    bad["billing_usage"]["openai_usage"]["input_tokens"] = 7.into();
    assert!(UsageEvidence::extract(Endpoint::OpenaiResponses, Some(&bad)).is_err());
    let mut obs = Observer::new(Endpoint::AnthropicMessages);
    obs.observe(None,&json!({"type":"message_start","message":{"id":"a","usage":{"input_tokens":0,"output_tokens":0,
        "cache_creation_input_tokens":0,"cache_read_input_tokens":0,"claude_cache_creation_5_m_tokens":0,
        "claude_cache_creation_1_h_tokens":0}}})).unwrap();
    obs.observe(None,&json!({"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{
        "input_tokens":33,"output_tokens":5,"cache_creation_input_tokens":0,"cache_read_input_tokens":0,
        "billing_usage":{"source":"gateway","semantic":"openai","openai_usage":canonical}}})).unwrap();
    let terminal = obs
        .observe(None, &json!({"type":"message_stop"}))
        .unwrap()
        .unwrap();
    assert_eq!(terminal.usage.raw_usage.unwrap()["input_tokens"], 33);
}

#[test]
fn custom_responses_tool_is_forwarded_without_json_coercion() {
    let body = json!({"model":"m","input":[{"type":"custom_tool_call","call_id":"c","name":"grammar","input":"a b"},
        {"type":"custom_tool_call_output","call_id":"c","output":"ok"}],
        "tools":[{"type":"custom","name":"grammar","format":{"type":"text"}}]});
    let req = Generate::parse(Endpoint::OpenaiResponses, body.clone(), false).unwrap();
    assert!(req.has_tools());
    let (wire, _) = req.prepare_native("deployment", 10, 10).unwrap();
    assert_eq!(wire["input"], body["input"]);
    assert_eq!(wire["tools"], body["tools"]);
    assert!(req.convert().is_err());
}

#[test]
fn remote_tools_keep_native_bindings_and_only_token_priced_usage() {
    let ep = Endpoint::AnthropicMessages;
    let body = json!({"model":"m","max_tokens":8,"messages":[{"role":"user","content":"read"}],
        "mcp_servers":[{"type":"url","name":"docs","url":"https://example.org/mcp"}],
        "tools":[{"type":"mcp_toolset","mcp_server_name":"docs"},{"type":"web_fetch_20250910","name":"web_fetch"}]});
    let request = Generate::parse(ep, body.clone(), false).unwrap();
    assert!(request.has_tools());
    assert_eq!(request.prepare_native("m", 64, 8).unwrap().0, body);
    let mut wrong = body;
    wrong["tools"][0]["mcp_server_name"] = "elsewhere".into();
    assert!(Generate::parse(ep, wrong, false).is_err());

    let mut counts =
        json!({"input_tokens":2,"output_tokens":3,"server_tool_use":{"web_fetch_requests":1}});
    assert!(nexus_relay::protocol::usage::UsageEvidence::extract(ep, Some(&counts)).is_ok());
    counts["server_tool_use"]["web_search_requests"] = 1.into();
    assert_eq!(
        nexus_relay::protocol::usage::UsageEvidence::extract(ep, Some(&counts))
            .unwrap()
            .raw_usage
            .unwrap()["web_search_requests"],
        1
    );

    let mut limits = nexus_relay::protocol::tools::ToolLimits::new(ep, 256);
    for kind in ["server_tool_use", "mcp_tool_use"] {
        limits
            .observe(&json!({"type":"content_block_start","index":1,"content_block":{"type":kind}}))
            .unwrap();
        limits.observe(&json!({"type":"content_block_delta","index":1,"delta":{"type":"input_json_delta","partial_json":"{}"}})).unwrap();
        limits
            .observe(&json!({"type":"content_block_stop","index":1}))
            .unwrap();
    }
}

#[test]
fn conflicting_search_receipts_cannot_be_settled_as_zero() {
    let mut observer = Observer::new(Endpoint::OpenaiResponses);
    observer
        .observe(
            None,
            &json!({"type":"response.created","response":{"id":"r"}}),
        )
        .unwrap();
    observer.observe(None,&json!({"type":"response.output_item.done","item":{"type":"web_search_call","id":"w","status":"completed","action":{"type":"search"}}})).unwrap();
    let end=observer.observe(None,&json!({"type":"response.completed","response":{"id":"r","status":"completed","output":[],"usage":{
        "input_tokens":1,"output_tokens":1,"input_tokens_details":{"cached_tokens":0}}}})).unwrap().unwrap();
    assert!(end.usage.raw_usage.is_none());
}

#[test]
fn streamed_web_call_cannot_be_reclassified_as_file_search() {
    let mut observer = Observer::new(Endpoint::OpenaiResponses);
    observer
        .observe(
            None,
            &json!({"type":"response.created","response":{"id":"r"}}),
        )
        .unwrap();
    observer
        .observe(
            None,
            &json!({"type":"response.web_search_call.in_progress","item_id":"one"}),
        )
        .unwrap();
    let done=observer.observe(None,&json!({"type":"response.completed","response":{
        "id":"r","status":"completed","output":[{"id":"one","type":"file_search_call","status":"completed","queries":["q"]}],
        "usage":{"input_tokens":1,"input_tokens_details":{"cached_tokens":0},"output_tokens":1}
    }})).unwrap().unwrap();
    assert!(done.usage.raw_usage.is_none());
}

#[test]
fn claude_iteration_usage_checks_cache_tariffs_models_and_message_totals() {
    let ordinary = json!({"type":"message","model":"actual","input_tokens":5,"output_tokens":3,"cache_read_input_tokens":1,"cache_creation_input_tokens":0});
    let compact = json!({"type":"compaction","input_tokens":7,"output_tokens":2,"cache_read_input_tokens":4,"cache_creation_input_tokens":3,
        "cache_creation":{"ephemeral_5m_input_tokens":1,"ephemeral_1h_input_tokens":2}});
    let usage = json!({"input_tokens":5,"output_tokens":3,"output_tokens_details":{"thinking_tokens":2},
        "cache_read_input_tokens":1,"cache_creation_input_tokens":0,"iterations":[compact,ordinary]});
    let mut msg = json!({"type":"message","id":"m","model":"actual","stop_reason":"end_turn","content":[{"type":"text","text":"ok"}],"usage":usage});
    let parse =
        |v: &Value| nexus_relay::protocol::response::observe(Endpoint::AnthropicMessages, v);
    let raw = parse(&msg).unwrap().usage.raw_usage.unwrap();
    assert_eq!(raw["input_tokens"], 12);
    assert_eq!(raw["output_tokens"], 5);
    assert_eq!(raw["cache_read_input_tokens"], 5);
    assert_eq!(raw["cache_write_1h_tokens"], 2);
    assert_eq!(raw["thinking_tokens"], 2);
    assert_eq!(raw["compaction_cache_creation_input_tokens"], 3);
    for (path, bad) in [
        (
            "/iterations/0/cache_creation/ephemeral_1h_input_tokens",
            json!(1),
        ),
        ("/iterations/1/model", json!("differently-priced")),
        ("/iterations/1/type", json!("unpriced")),
        ("/output_tokens", json!(4)),
        ("/output_tokens_details/thinking_tokens", json!(4)),
    ] {
        msg["usage"] = usage.clone();
        *msg["usage"].pointer_mut(path).unwrap() = bad;
        assert!(parse(&msg).is_err());
    }
    msg["usage"] = usage;
    msg["usage"]
        .as_object_mut()
        .unwrap()
        .remove("output_tokens");
    assert!(parse(&msg).unwrap().usage.raw_usage.is_none());
}

#[test]
fn every_model_call_of_a_tool_loop_or_compaction_is_reserved() {
    let interactive = CallLoop::Messages { batch: false };
    let batch = CallLoop::Messages { batch: true };
    let calls = |kind, mut body: Value| model_calls(kind, false, &mut body);
    let search = |n: u64| json!({"type":"web_search_20250305","name":"web_search","max_uses":n});
    let compact = json!({"edits":[{"type":"compact_20260112"}]});
    // Client tools return to the caller: one call.
    let client =
        json!({"tools":[{"name":"f","input_schema":{}},{"type":"bash_20250124","name":"bash"}]});
    assert_eq!(calls(interactive, client).unwrap(), 1);
    // Each permitted search can add one sampling iteration that re-reads the prompt.
    assert_eq!(calls(interactive, json!({"tools":[search(2)]})).unwrap(), 3);
    assert_eq!(
        calls(
            interactive,
            json!({"tools":[search(2)],"context_management":compact})
        )
        .unwrap(),
        6
    );
    // Anthropic stops an interactive server loop after its documented iteration limit.
    assert_eq!(
        calls(interactive, json!({"tools":[search(40)]})).unwrap(),
        SERVER_LOOP_LIMIT
    );
    assert_eq!(calls(batch, json!({"tools":[search(40)]})).unwrap(), 41);
    for tool in [
        json!({"type":"code_execution_20250825","name":"code_execution"}),
        json!({"type":"web_fetch_20260209","name":"web_fetch","max_uses":2}),
        json!({"type":"web_search_20260209","name":"web_search","max_uses":2}),
    ] {
        let body = json!({"tools":[search(1), tool]});
        assert_eq!(calls(interactive, body.clone()).unwrap(), SERVER_LOOP_LIMIT);
        // A batch has no documented loop limit, so an uncapped loop is rejected.
        assert!(calls(batch, body).is_err());
    }
    assert!(calls(batch, json!({"mcp_servers":[{"type":"url"}]})).is_err());
    assert_eq!(
        calls(interactive, json!({"context_management":compact})).unwrap(),
        2
    );

    let mut open = json!({"tools":[{"type":"code_interpreter","container":{"type":"auto"}}]});
    assert_eq!(
        model_calls(CallLoop::Responses, false, &mut open).unwrap(),
        DEFAULT_TOOL_CALLS + 1
    );
    assert_eq!(open["max_tool_calls"], DEFAULT_TOOL_CALLS);
    let mut functions = json!({"tools":[{"type":"function","name":"f"}]});
    assert_eq!(
        model_calls(CallLoop::Responses, false, &mut functions).unwrap(),
        1
    );
    assert!(functions.get("max_tool_calls").is_none());
    let capped = json!({"tools":[{"type":"web_search"}],"max_tool_calls":3,
        "context_management":[{"type":"compaction","compact_threshold":1000}]});
    assert_eq!(calls(CallLoop::Responses, capped).unwrap(), 8);
    assert!(
        calls(
            CallLoop::Responses,
            json!({"tools":[{"type":"file_search"}],"max_tool_calls":64})
        )
        .is_err()
    );
    assert_eq!(
        calls(CallLoop::Single, json!({"tools":[search(5)]})).unwrap(),
        1
    );
    assert_eq!(
        model_calls(
            interactive,
            true,
            &mut json!({"context_management":compact})
        )
        .unwrap(),
        1
    );

    let mut bounds = ReservationBounds::for_deployment(200_000, 8, None).unwrap();
    bounds.cover_model_calls(6).unwrap();
    assert_eq!(bounds.inputs, 1_200_000);
    assert!(bounds.cover_model_calls(1_000).is_err());
}
