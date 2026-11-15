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

#[test]
fn portable_media_and_signed_thinking_keep_their_data() {
    let image =
        json!({"type":"image","source":{"type":"base64","media_type":"image/png","data":"aGk="}});
    let pdf = json!({"type":"document","title":"report","source":{"type":"base64","media_type":"application/pdf","data":"cGRm"}});
    let src = json!({"model":"public","max_tokens":4096,"thinking":{"type":"enabled","budget_tokens":1024},
        "messages":[{"role":"user","content":[image.clone(),pdf.clone(),{"type":"text","text":"read this"}]},
        {"role":"assistant","content":[{"type":"thinking","thinking":"signed text","signature":"sig"},{"type":"text","text":"ok"}]},
        {"role":"user","content":"continue"}]});
    let parsed = Generate::parse(Endpoint::AnthropicMessages, src, false).unwrap();
    let req = bedrock::prepare(&parsed).unwrap();
    assert_eq!(
        req["additionalModelRequestFields"]["thinking"]["budget_tokens"],
        1024
    );
    assert_eq!(
        req["messages"][0]["content"][0]["image"]["source"]["bytes"],
        "aGk="
    );
    assert_eq!(
        req["messages"][0]["content"][1]["document"]["format"],
        "pdf"
    );
    let thinking = req["messages"][1]["content"][0].clone();
    assert_eq!(
        thinking["reasoningContent"]["reasoningText"]["signature"],
        "sig"
    );
    let reply=wire::message(&json!({"output":{"message":{"role":"assistant","content":[thinking,{"text":"answer"}]}},"stopReason":"end_turn"}),"r","m",&wire::evidence(None)).unwrap();
    assert_eq!(
        reply["content"][0],
        json!({"type":"thinking","thinking":"signed text","signature":"sig"})
    );
    let args =
        json!({"model":"public","max_tokens":4,"messages":[{"role":"user","content":[image,pdf]}]});
    let responses =
        nexus_relay::protocol::convert::request::convert(Endpoint::AnthropicMessages, &args)
            .unwrap();
    let parts = &responses["input"][0]["content"];
    assert_eq!(parts[0]["image_url"], "data:image/png;base64,aGk=");
    assert_eq!(parts[1]["filename"], "report");
    let back =
        nexus_relay::protocol::convert::request::convert(Endpoint::OpenaiResponses, &responses)
            .unwrap();
    assert_eq!(back["messages"], args["messages"]);
}

#[test]
fn matched_stop_sequences_are_requested_and_preserved() {
    let parsed=Generate::parse(Endpoint::AnthropicMessages,json!({"model":"m","max_tokens":4,"messages":[{"role":"user","content":"go"}],"stop_sequences":["END","NEXT"]}),false).unwrap();
    let req = bedrock::prepare(&parsed).unwrap();
    assert_eq!(
        req["additionalModelResponseFieldPaths"],
        json!(["/stop_sequence"])
    );
    let mut reply = json!({"output":{"message":{"role":"assistant","content":[{"text":"partial"}]}},"stopReason":"stop_sequence","additionalModelResponseFields":{"stop_sequence":"NEXT"}});
    let message =
        |v: &serde_json::Value| wire::message(v, "r", "m", &wire::evidence(None)).unwrap();
    assert_eq!(message(&reply)["stop_sequence"], "NEXT");
    reply
        .as_object_mut()
        .unwrap()
        .remove("additionalModelResponseFields");
    assert!(message(&reply)["stop_sequence"].is_null());
}

#[test]
fn inline_video_and_office_files_follow_the_selected_converse_transport() {
    let src = json!({"model":"m","input":[{"role":"user","content":[
        {"type":"input_file","filename":"q2.xlsx","file_data":"data:application/vnd.openxmlformats-officedocument.spreadsheetml.sheet;base64,YWJj"},
        {"type":"input_file","filename":"v.mov","file_data":"data:video/quicktime;base64,YWJj"},
        {"type":"input_text","text":"describe"}]}]});
    let r = Generate::parse(Endpoint::OpenaiResponses, src.clone(), false).unwrap();
    assert!(r.convert().is_err());
    for prepared in [bedrock::prepare(&r), bedrock::prepare_cached(&r)] {
        let body = prepared.unwrap();
        let blocks = &body["messages"][0]["content"];
        assert_eq!(
            blocks[0]["document"],
            json!({"format":"xlsx","name":"q2 xlsx","source":{"bytes":"YWJj"}})
        );
        assert_eq!(
            blocks[1]["video"],
            json!({"format":"mov","source":{"bytes":"YWJj"}})
        );
    }
    let mut bad = src;
    bad["input"][0]["content"][1]["file_data"] = "data:video/mp4;base64,!".into();
    let req = Generate::parse(Endpoint::OpenaiResponses, bad, false).unwrap();
    assert!(bedrock::prepare(&req).is_err());
}

#[test]
fn assistant_prefill_keeps_the_last_text_without_adding_a_turn() {
    let request=Generate::parse(Endpoint::AnthropicMessages,json!({"model":"m","max_tokens":8,"messages":[{"role":"user","content":"Complete the sentence."},{"role":"assistant","content":"The answer is"}]}),false).unwrap();
    let wire = bedrock::prepare(&request).unwrap();
    assert_eq!(wire["messages"].as_array().unwrap().len(), 2);
    assert_eq!(
        wire["messages"][1],
        json!({"role":"assistant","content":[{"text":"The answer is"}]})
    );
    assert!(request.convert().is_err());
}
