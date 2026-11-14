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
