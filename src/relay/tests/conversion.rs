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
