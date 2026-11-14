use nexus_relay::protocol::{
    Endpoint::{AnthropicMessages as M, OpenaiResponses as R},
    ProtocolError,
    convert::{Exchange, request, response, stream::Stream},
    stream::Observer,
    usage::UsageEvidence,
};
use serde_json::{Value, json};

#[test]
fn explicit_history_and_parallel_tool_results_keep_their_call_ids() {
    let msg = json!({"model":"pub","max_tokens":16,"system":[{"type":"text","text":"s"}],
        "messages":[{"role":"user","content":"go"},{"role":"assistant","content":[{"type":"text","text":"run"},
            {"type":"tool_use","id":"a","name":"f","input":{"x":1234567890123456789_i64}},
            {"type":"tool_use","id":"b","name":"f","input":{}}]},
        {"role":"user","content":[{"type":"tool_result","tool_use_id":"b","content":[{"type":"text","text":"result b"}]},
            {"type":"tool_result","tool_use_id":"a","content":"result a"},{"type":"text","text":"next"}]}],
        "tools":[{"name":"f","input_schema":{"type":"object","properties":{}}}],"tool_choice":{"type":"tool","name":"f","disable_parallel_tool_use":true}});
    let res = request::convert(M, &msg).unwrap();
    assert_eq!(res["instructions"], "s");
    assert_eq!(res["input"][2]["call_id"], "a");
    assert_eq!(res["input"][3]["arguments"], "{}");
    assert_eq!(res["input"][4]["call_id"], "b");
    assert_eq!(res["input"][5]["output"], "result a");
    assert_eq!(res["tools"][0]["strict"], false);
    assert_eq!(res["parallel_tool_calls"], false);
    assert_eq!(res["tool_choice"], json!({"type":"function","name":"f"}));
    let back = request::convert(R, &res).unwrap();
    assert_eq!(back["messages"][1], msg["messages"][1]);
    assert_eq!(back["messages"][2], msg["messages"][2]);
    assert_eq!(back["tool_choice"], msg["tool_choice"]);
}

#[test]
fn reject_lossy_history_and_unknown_options_before_reservation() {
    let base = json!({"model":"m","messages":[{"role":"user","content":"hi"}],"max_tokens":9});
    for (k, v) in [
        ("stop_sequences", json!(["stop"])),
        ("thinking", json!({"type":"enabled"})),
        ("top_k", json!(4)),
    ] {
        let mut b = base.clone();
        b[k] = v;
        assert!(request::convert(M, &b).is_err());
    }
    for content in [
        json!([{"type":"tool_result","tool_use_id":"foreign","content":"x"}]),
        json!([{"type":"image","source":{}}]),
        json!([{"type":"text","text":"x","cache_control":{"type":"ephemeral"}}]),
    ] {
        let mut b = base.clone();
        b["messages"][0]["content"] = content;
        assert!(request::convert(M, &b).is_err());
    }
    let calls = json!([{"type":"function_call","call_id":"x","name":"f","arguments":"{}"},
        {"type":"function_call","call_id":"x","name":"f","arguments":"{}"},{"type":"function_call_output","call_id":"x","output":"x"}]);
    for input in [
        calls,
        json!([{"type":"reasoning","encrypted_content":"private"}]),
        json!([{"type":"function_call","name":"f","call_id":"a","arguments":"[]"}]),
        json!([{"type":"function_call_output","call_id":"no-parent","output":"hi"}]),
        json!([{"role":"user","content":"hi"},{"role":"developer","content":"late"}]),
    ] {
        assert!(request::convert(R, &json!({"input":input})).is_err());
    }
    assert!(request::convert(R,&json!({"input":"ok","tools":[{"type":"function","name":"f","parameters":{"type":"object","properties":{"x":{"type":"number"}}}}]})).is_err());
    assert!(request::convert(R,&json!({"input":"ok","tools":[{"type":"function","name":"f","parameters":{"type":"object","properties":{},"required":[],"additionalProperties":false}}]})).is_ok());
}

#[test]
fn mapped_stop_errors_and_usage_keep_native_semantics() {
    let msg = json!({"id":"m","content":[{"type":"text","text":"hi"}],"stop_reason":"max_tokens"});
    let raw = json!({"input_tokens":10,"output_tokens":3,"cache_creation_input_tokens":2,"cache_read_input_tokens":5});
    let evidence = UsageEvidence::extract(M, Some(&raw)).unwrap();
    let x = Exchange {
        client: R,
        upstream: M,
        public_model: "alias",
    };
    let (r, status) = response::convert(x, &msg, &evidence, 200).unwrap();
    assert_eq!(status, 200);
    assert_eq!(r["status"], "incomplete");
    assert_eq!(r["incomplete_details"]["reason"], "max_output_tokens");
    assert_eq!(r["usage"]["total_tokens"], 20);
    assert_eq!(evidence.raw_usage.unwrap()["input_tokens"], 10);
    let raw = json!({"input_tokens":17,"output_tokens":3,"input_tokens_details":{"cached_tokens":5,"cache_write_tokens":2}});
    let evidence = UsageEvidence::extract(R, Some(&raw)).unwrap();
    let x = Exchange {
        client: M,
        upstream: R,
        public_model: "alias",
    };
    let (m, _) = response::convert(x, &r, &evidence, 200).unwrap();
    assert_eq!(m["stop_reason"], "max_tokens");
    assert_eq!(m["usage"]["input_tokens"], 10);
    assert_eq!(m["usage"]["cache_read_input_tokens"], 5);
    let (m, _) = response::convert(x, &r, &UsageEvidence::extract(R, None).unwrap(), 200).unwrap();
    assert_eq!(m["usage"], json!({"input_tokens":0,"output_tokens":0}));
    let err = response::convert(
        x,
        &json!({"object":"response","status":"failed","error":{"message":"failed"}}),
        &evidence,
        200,
    )
    .unwrap();
    assert_eq!(err.1, 502);
    assert_eq!(err.0["error"]["message"], "failed");
    let mut r = r;
    r["output"][0]["content"][0] = json!({"type":"refusal","refusal":"no"});
    assert!(response::convert(x, &r, &evidence, 200).is_err());
}

fn step(
    stream: &mut Stream,
    observer: &mut Observer,
    v: Value,
) -> Result<Vec<Value>, ProtocolError> {
    let done = observer.observe(None, &v)?;
    stream.event(&v, done.as_ref())
}

#[test]
fn final_only_responses_items_reconcile_and_bad_final_data_cannot_complete() {
    for wrong in [false, true] {
        let mut s = Stream::new(M, "alias", 2048);
        let mut o = Observer::new(R);
        step(
            &mut s,
            &mut o,
            json!({"type":"response.created","response":{"id":"r","status":"in_progress"}}),
        )
        .unwrap();
        let item =
            json!({"type":"function_call","id":"item","call_id":"call","name":"f","arguments":""});
        step(
            &mut s,
            &mut o,
            json!({"type":"response.output_item.added","output_index":0,"item":item}),
        )
        .unwrap();
        step(&mut s,&mut o,json!({"type":"response.function_call_arguments.delta","output_index":0,"item_id":"item","delta":"{\"q\":"})).unwrap();
        let mut item = item;
        item["arguments"] = if wrong { "{}" } else { "{\"q\":42}" }.into();
        let events = step(
            &mut s,
            &mut o,
            json!({"type":"response.completed","response":{"id":"r","status":"completed","output":[item,{"type":"message","id":"m","role":"assistant","content":[{"type":"output_text","text":"tail","annotations":[]}]}]}}),
        );
        if wrong {
            assert!(events.is_err());
        } else {
            let events = events.unwrap();
            assert_eq!(events[0]["delta"]["partial_json"], "42}");
            assert_eq!(events.last().unwrap()["type"], "message_stop");
            assert!(events.iter().any(|e| e["delta"]["text"] == "tail"));
        }
    }
}

#[test]
fn converted_stream_budget_and_missing_terminal_never_synthesize_success() {
    let mut s = Stream::new(R, "alias", 256);
    let mut o = Observer::new(M);
    let mut all = step(
        &mut s,
        &mut o,
        json!({"type":"message_start","message":{"id":"m","usage":null}}),
    )
    .unwrap();
    all.extend(step(&mut s,&mut o,json!({"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}})).unwrap());
    assert_eq!(
        step(
            &mut s,
            &mut o,
            json!({"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"x".repeat(257)}})
        ),
        Err(ProtocolError::EventTooLarge)
    );
    assert!(!all.iter().any(|v| v["type"] == "response.completed"));
    assert!(o.finish().is_err());
}

#[test]
fn errors_use_client_stream_schema_and_never_emit_a_success_terminal() {
    for (source, target) in [(R, M), (M, R)] {
        let mut stream = Stream::new(target, "public", 4096);
        let mut observer = Observer::new(source);
        let mut frames = step(
            &mut stream,
            &mut observer,
            json!({"type":"error","error":{"type":"overloaded_error","message":"busy"}}),
        )
        .unwrap();
        let e = frames.pop().unwrap();
        assert!(frames.is_empty());
        assert_eq!(e["type"], "error");
        assert_eq!(
            if target == R {
                &e["message"]
            } else {
                &e["error"]["message"]
            },
            "busy"
        );
        assert!(stream.event(&json!({"type":"message_stop"}), None).is_err());
    }
}

#[test]
fn early_truncation_and_conflicting_item_identity_do_not_emit_final_responses() {
    let mut s = Stream::new(R, "m", 4096);
    let mut observer = Observer::new(M);
    step(
        &mut s,
        &mut observer,
        json!({"type":"message_start","message":{"id":"m"}}),
    )
    .unwrap();
    step(&mut s,&mut observer,json!({"type":"content_block_start","index":0,"content_block":{"type":"tool_use","id":"bound","name":"f","input":{}}})).unwrap();
    step(&mut s,&mut observer,json!({"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":"{"}})).unwrap();
    assert!(
        step(
            &mut s,
            &mut observer,
            json!({"type":"content_block_stop","index":0})
        )
        .is_err()
    );
    let mut s = Stream::new(M, "m", 4096);
    let mut observer = Observer::new(R);
    step(
        &mut s,
        &mut observer,
        json!({"type":"response.created","response":{"id":"r","status":"in_progress"}}),
    )
    .unwrap();
    step(&mut s,&mut observer,json!({"type":"response.output_item.added","output_index":0,"item":{"type":"function_call","id":"item","call_id":"c","name":"f","arguments":""}})).unwrap();
    assert!(step(&mut s,&mut observer,json!({"type":"response.function_call_arguments.delta","output_index":0,"item_id":"wrong","delta":"{}"})).is_err());
}

#[test]
fn disabling_tools_without_declarations_requires_no_tool_capability() {
    use nexus_relay::protocol::request::Generate;
    for (ep, body) in [
        (
            M,
            json!({"model":"m","messages":[{"role":"user","content":"hi"}],"max_tokens":8,"tool_choice":{"type":"none"}}),
        ),
        (R, json!({"model":"m","input":"hi","tool_choice":"none"})),
    ] {
        let req = Generate::parse(ep, body, false).unwrap();
        assert!(!req.has_tools());
        assert!(req.convert().unwrap().get("tool_choice").is_none());
    }
}

#[test]
fn implicit_responses_tools_close_fully_required_object_schemas() {
    let schema = json!({"type":"object","properties":{"x":{"type":"integer"}},"required":["x"]});
    let body = json!({"model":"m","input":"test","tools":[{"type":"function","name":"f","parameters":schema}]});
    let out = request::convert(R, &body).unwrap();
    assert_eq!(out["tools"][0]["strict"], true);
    assert_eq!(
        out["tools"][0]["input_schema"]["additionalProperties"],
        false
    );
    assert_eq!(
        out["tools"][0]["input_schema"]["required"],
        schema["required"]
    );
}

#[test]
fn cache_controls_preserve_explicit_boundaries_and_user_content() {
    use nexus_relay::protocol::cache;
    let src = json!({"model":"x","input":[{"role":"developer","content":[{"type":"input_text","text":"policy","prompt_cache_breakpoint":{"mode":"explicit"}}]},
        {"role":"user","content":[{"type":"input_text","text":"hi"}]}],
        "prompt_cache_options":{"mode":"explicit"}, "prompt_cache_key":"tenant-private"});
    let out = cache::to_messages(&src).unwrap();
    assert_eq!(
        out["system"],
        json!([{"type":"text","text":"policy","cache_control":{"type":"ephemeral"}}])
    );
    assert!(out.get("cache_control").is_none() && out.get("prompt_cache_key").is_none());
    assert_eq!(out["messages"][0]["content"][0]["text"], "hi");
    let mut unsupported = src.clone();
    unsupported["prompt_cache_options"]["ttl"] = "12h".into();
    assert!(cache::to_messages(&unsupported).is_err());
    let blank =
        cache::to_messages(&json!({"input":"hi","prompt_cache_options":{"mode":"explicit"}}))
            .unwrap();
    assert!(blank.get("cache_control").is_none());
    assert_eq!(
        cache::to_messages(&json!({"input":"hi","prompt_cache_key":"group"})).unwrap()["cache_control"]
            ["type"],
        "ephemeral"
    );
}

#[test]
fn bedrock_cache_points_follow_marked_blocks_and_only_use_the_selected_ttl() {
    use nexus_relay::{bedrock, protocol::request::Generate};
    let body = json!({"model":"m","max_tokens":6,"system":[{"type":"text","text":"policy","cache_control":{"type":"ephemeral","ttl":"1h"}}],
        "tools":[{"name":"f","input_schema":{"type":"object"},"cache_control":{"type":"ephemeral"}}],
        "messages":[{"role":"user","content":[{"type":"text","text":"reference","cache_control":{"type":"ephemeral"}},{"type":"text","text":"question"}]}]});
    let req = Generate::parse(M, body, false).unwrap();
    let v = bedrock::prepare_cached(&req).unwrap();
    assert_eq!(
        v["system"][1],
        json!({"cachePoint":{"type":"default","ttl":"1h"}})
    );
    assert_eq!(v["toolConfig"]["tools"][1]["cachePoint"]["type"], "default");
    assert_eq!(
        v["messages"][0]["content"],
        json!([{"text":"reference"},{"cachePoint":{"type":"default"}},{"text":"question"}])
    );
    assert!(bedrock::prepare(&req).is_err());
}

#[test]
fn affinity_is_tenant_scoped_and_header_is_bounded_and_unambiguous() {
    use http::{HeaderMap, HeaderValue};
    use nexus_relay::protocol::{cache, request::cache_key_header};
    use uuid::Uuid;
    let org = Uuid::new_v4();
    let other = Uuid::new_v4();
    let model = Uuid::new_v4();
    let b = json!({"input":"hello","prompt_cache_key":"shared"});
    assert_ne!(
        cache::affinity(org, model, R, &b, None, true),
        cache::affinity(other, model, R, &b, None, true)
    );
    let mut headers = HeaderMap::new();
    headers.insert("x-nexus-cache-key", HeaderValue::from_static("client-key"));
    assert_eq!(
        cache_key_header(&headers).unwrap().as_deref(),
        Some("client-key")
    );
    headers.append("x-nexus-cache-key", HeaderValue::from_static("second"));
    assert!(cache_key_header(&headers).is_err());
    assert!(!cache::valid_key(&"x".repeat(257)));
}

#[test]
fn text_breakpoints_remain_on_the_same_part_across_protocols() {
    use nexus_relay::protocol::cache;
    let src = json!({"max_tokens":8,"messages":[{"role":"user","content":[
        {"type":"text","text":"long reference","cache_control":{"type":"ephemeral"}},
        {"type":"text","text":"changes every turn"}]}]});
    let r = cache::to_responses(&src).unwrap();
    let parts = r["input"][0]["content"].as_array().unwrap();
    assert_eq!(parts[0]["prompt_cache_breakpoint"]["mode"], "explicit");
    assert!(parts[1].get("prompt_cache_breakpoint").is_none());
    let back = cache::to_messages(&r).unwrap();
    assert_eq!(back["messages"], src["messages"]);
    assert!(back.get("cache_control").is_none());
    let mut invalid = src.clone();
    invalid["messages"][0]["content"][0]["cache_control"]["ttl"] = "1h".into();
    assert!(cache::to_responses(&invalid).is_err());
    invalid["messages"][0]["content"][0]["cache_control"]["ttl"] = "5m".into();
    assert!(cache::to_responses(&invalid).is_err());
}

#[test]
fn shared_templates_reuse_placement_without_requiring_a_conversation_id() {
    use nexus_relay::protocol::cache;
    use uuid::Uuid;
    let org = Uuid::new_v4();
    let model = Uuid::new_v4();
    let stable = "shared runbook ".repeat(180);
    let a = json!({"instructions":stable,"input":"one question"});
    let b = json!({"instructions":stable,"input":"different question"});
    assert_eq!(
        cache::affinity(org, model, R, &a, None, true),
        cache::affinity(org, model, R, &b, None, true)
    );
    assert!(cache::affinity(org, model, R, &a, None, false).is_none());
    assert_ne!(
        cache::affinity(org, model, R, &a, Some("a"), true),
        cache::affinity(org, model, R, &a, Some("b"), true)
    );
}

#[test]
fn cached_tool_result_keeps_the_call_id_and_endpoint_of_the_cached_history() {
    use nexus_relay::protocol::cache;
    let src = json!({"max_tokens":16,"messages":[{"role":"user","content":"lookup"},
        {"role":"assistant","content":[{"type":"tool_use","name":"f","id":"call","input":{}}]},
        {"role":"user","content":[{"type":"tool_result","tool_use_id":"call","content":"answer","cache_control":{"type":"ephemeral"}},{"type":"text","text":"next"}]}]});
    let response = cache::to_responses(&src).unwrap();
    assert_eq!(response["input"][2]["call_id"], "call");
    assert_eq!(
        response["input"][2]["output"][0]["prompt_cache_breakpoint"]["mode"],
        "explicit"
    );
    let back = cache::to_messages(&response).unwrap();
    assert_eq!(back["messages"][2]["content"][0]["tool_use_id"], "call");
    assert_eq!(
        back["messages"][2]["content"][0]["cache_control"]["type"],
        "ephemeral"
    );
    assert_eq!(
        back["messages"][2]["content"][1],
        src["messages"][2]["content"][1]
    );
}

#[test]
fn bedrock_cached_request_validation_rejects_non_message_elements() {
    use nexus_relay::{bedrock, protocol::request::Generate};
    for invalid in [json!(42), json!(null), json!({}), json!({"role":"user"})] {
        let req = Generate::parse(
            M,
            json!({"model":"m","max_tokens":1,"messages":[invalid]}),
            false,
        )
        .unwrap();
        assert!(bedrock::prepare_cached(&req).is_err());
    }
}
