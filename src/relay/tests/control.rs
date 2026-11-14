use std::{env, time::Duration};

use base64::{Engine, engine::general_purpose::STANDARD};
use crypto_box::SecretKey;
use nexus_relay::{
    control::{credential::CredentialRef, database::Gateway},
    protocol::tokens::ReservationBounds,
};
use serde::Deserialize;
use sha2::{Digest, Sha256};
use uuid::Uuid;

#[derive(Deserialize)]
struct Inputs {
    url: String,
    cluster: Uuid,
    key: Uuid,
    model: Uuid,
    deployment: Uuid,
    request: Uuid,
    unstarted: Uuid,
    attempt: Uuid,
    private: String,
    secret_digest: String,
    events: Vec<String>,
}

#[tokio::test]
#[ignore = "run by tests/integration/test_plane.py against a provisioned isolated gateway role"]
async fn isolated_gateway_contract() -> Result<(), Box<dyn std::error::Error>> {
    let input: Inputs = serde_json::from_str(&env::var("NEXUS_RELAY_TEST_INPUT")?)?;
    let gateway = Gateway::connect(&input.url, 2, Duration::from_secs(3)).await?;
    let snapshot = gateway.snapshot(input.cluster, None).await?;
    let caps = [
        "reservation.v1",
        "usage-events.v1",
        "relay.native.v1",
        "relay.responses-ws.v1",
    ];
    assert!(
        gateway
            .acknowledge(&snapshot, &caps[..2], None)
            .await
            .is_err()
    );
    gateway.acknowledge(&snapshot, &caps, None).await?;
    let reference: CredentialRef =
        serde_json::from_value(snapshot.resources["credential_refs"][0].clone())?;
    let envelope = gateway.credential(snapshot.revision, &reference).await?;
    let bytes: [u8; 32] = STANDARD
        .decode(input.private)?
        .try_into()
        .map_err(|_| "invalid fixture")?;
    let key = SecretKey::from(bytes);
    assert!(envelope.open(Uuid::nil(), &reference, &key).is_err());
    let secret = envelope.open(input.cluster, &reference, &key)?;
    assert_eq!(
        format!("{:x}", Sha256::digest(secret.as_bytes())),
        input.secret_digest
    );

    let bounds = ReservationBounds::for_deployment(4, 6, None)?;
    assert!(gateway.status(input.request).await?.is_none());
    gateway
        .reserve(
            input.unstarted,
            input.key,
            input.model,
            snapshot.revision,
            bounds,
        )
        .await?;
    assert_eq!(
        gateway
            .cancel_unstarted(input.unstarted, "test_cancel")
            .await?["state"],
        "canceled"
    );
    for replay in [false, true] {
        let reply = gateway
            .reserve(
                input.request,
                input.key,
                input.model,
                snapshot.revision,
                bounds,
            )
            .await?;
        assert_eq!(reply["replayed"], replay);
        let reply = gateway
            .start_attempt(input.request, input.attempt, 1, input.deployment)
            .await?;
        assert_eq!(reply["replayed"], replay);
    }
    for event in input.events {
        for _ in 0..2 {
            assert_eq!(gateway.append_event(&event).await?["status"], "accepted");
        }
    }
    assert_eq!(
        gateway.status(input.request).await?.unwrap()["closed"],
        true
    );
    Ok(())
}

#[derive(Deserialize)]
struct LeaseInput {
    url: String,
    redis: String,
    namespace: String,
    cluster: Uuid,
    key_id: Uuid,
    request: Uuid,
    model: Uuid,
    revision: i64,
    digest: String,
    expired: bool,
}

#[tokio::test]
#[ignore = "paired with the Python quota client in the isolated integration suite"]
async fn cross_language_lease() -> Result<(), Box<dyn std::error::Error>> {
    use nexus_relay::control::{
        limits::{Admission, LimitError, Limits},
        snapshot::{Key, Quota},
    };
    let input: LeaseInput = serde_json::from_str(&env::var("NEXUS_RELAY_TEST_INPUT")?)?;
    let gateway = Gateway::connect(&input.url, 2, Duration::from_secs(3)).await?;
    let snapshot = gateway.snapshot(input.cluster, None).await?;
    assert_eq!(snapshot.digest, input.digest);
    assert_eq!(snapshot.revision, input.revision);
    let raw = snapshot.resources["key_verifiers"]
        .as_array()
        .unwrap()
        .iter()
        .find(|k| k["id"] == input.key_id.to_string())
        .unwrap();
    let key: Key = serde_json::from_value(raw.clone())?;
    let quotas: Vec<Quota> = serde_json::from_value(snapshot.resources["quota_policies"].clone())?;
    let limits = Limits::connect(&input.redis, input.namespace).await?;
    let admission = |request| Admission {
        key: &key,
        request,
        node: snapshot.node_id,
        model: input.model,
        revision: snapshot.revision,
        bounds: ReservationBounds {
            inputs: 4,
            outputs: 6,
        },
        quotas: &quotas,
        ttl_ms: 10000,
    };
    if input.expired {
        assert!(matches!(
            limits.acquire(admission(input.request)).await,
            Err(LimitError::Expired)
        ));
    } else {
        let mut lease = limits.acquire(admission(input.request)).await?;
        limits.renew(&mut lease).await?;
        assert!(matches!(
            limits.acquire(admission(Uuid::new_v4())).await,
            Err(LimitError::Limited)
        ));
        limits.finish(&lease, Some(5)).await?;
        limits.finish(&lease, Some(5)).await?;
    }
    Ok(())
}

#[test]
fn indexed_route_requires_the_scoped_key_and_selects_the_first_eligible_priority() {
    use nexus_relay::{
        control::{database::Snapshot, snapshot::Index},
        protocol::{Endpoint, request::Generate},
    };
    use serde_json::json;
    let (org, key, project, model, credential, primary, backup, other, legacy) = (
        Uuid::new_v4(),
        Uuid::new_v4(),
        Uuid::new_v4(),
        Uuid::new_v4(),
        Uuid::new_v4(),
        Uuid::new_v4(),
        Uuid::new_v4(),
        Uuid::new_v4(),
        Uuid::new_v4(),
    );
    let deploy = |id, endpoint, family, format| {
        json!({"id":id,"model_id":model,"upstream_model":"actual","base_url":"https://upstream.example/v1",
            "credential_id":credential,"credential_version":2,"protocol_family":family,"usage_format":format,
            "relay_profile":{"endpoint":endpoint,"input_ceiling":80,"output_ceiling":10,"tools":false,
            "transports":["http"],"count_rule":"deployment_ceiling.v1","bound_reference":"fixture"}})
    };
    let mut old = deploy(
        legacy,
        "openai_responses",
        "openai_compatible",
        "openai.responses.v1",
    );
    old.as_object_mut().unwrap().remove("relay_profile");
    old.as_object_mut().unwrap().remove("usage_format");
    let resources = json!({
        "key_verifiers":[{"id":key,"project_id":project,"organization_id":org,"digest":format!("{:x}",Sha256::digest(b"enterprise")),"expires_at":null}],
        "models":[{"id":model,"public_name":"public","max_input_tokens":80,"max_output_tokens":10,"capabilities":{"tools":true}}],
        "routes":[{"organization_id":org,"model_id":model,"candidates":[
            {"deployment_id":legacy,"priority":0,"weight":1},{"deployment_id":other,"priority":0,"weight":1},
            {"deployment_id":backup,"priority":2,"weight":99},{"deployment_id":primary,"priority":1,"weight":1}]}],
        "access_policies":[{"organization_id":org,"scope_id":org,"scope_type":"organization","layer":"platform","allowed_models":[model]}],
        "quota_policies":[],"credential_refs":[{"credential_id":credential,"credential_version":2,"key_version":1,"algorithm":"libsodium-sealed-box"}],
        "deployments":[old,deploy(other,"anthropic_messages","anthropic","anthropic.messages.v1"),
            deploy(primary,"openai_responses","openai_compatible","openai.responses.v1"),deploy(backup,"openai_responses","openai_compatible","openai.responses.v1")]
    });
    let mut snapshot = Snapshot {
        applied_revision: 0,
        node_id: Uuid::new_v4(),
        cluster_id: Uuid::new_v4(),
        revision: 1,
        digest: String::new(),
        required_capabilities: vec!["relay.native.v1".into()],
        resources: resources.clone(),
    };
    let index = Index::build(&mut snapshot).unwrap();
    let generate = |body| Generate::parse(Endpoint::OpenaiResponses, body, false).unwrap();
    let request = generate(json!({"model":"public","input":"hello"}));
    assert!(index.select("unknown", &request).is_err());
    let choice = index.select("enterprise", &request).unwrap();
    assert_eq!(choice.deployment.id, primary);
    assert_eq!(choice.bounds.inputs, 80);
    assert_eq!(choice.body["max_output_tokens"], 10);
    assert!(
        index
            .select(
                "enterprise",
                &generate(
                    json!({"model":"public","input":"hi","tools":[{"type":"function","name":"f"}]})
                )
            )
            .is_err()
    );
    snapshot.resources = resources;
    snapshot.resources["access_policies"].as_array_mut().unwrap().push(json!({
        "organization_id":org,"scope_id":key,"scope_type":"key","layer":"customer","allowed_models":[]}));
    assert!(
        Index::build(&mut snapshot)
            .unwrap()
            .select("enterprise", &request)
            .is_err()
    );
}
