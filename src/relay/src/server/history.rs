//! Validate converted client-carried state and reauthorize its referenced resources.
use super::Node;
use crate::{
    control::snapshot::Index,
    protocol::{Endpoint, request::Generate},
    request::Failure,
};
use http::StatusCode;
use serde_json::Value;

pub(super) async fn bind(
    node: &Node,
    index: &Index,
    raw: &str,
    request: &mut Generate,
) -> Result<(), Failure> {
    if request.has_private_history() {
        let kid = index.authenticate(raw)?.id;
        request
            .bind_history(&node.history, index, kid)
            .map_err(|_| Failure(StatusCode::BAD_REQUEST, "invalid_private_history"))?;
        let target = request.stored_binding.ok_or_else(Failure::unavailable)?;
        for item in request.restored.originals() {
            match request.endpoint {
                Endpoint::AnthropicMessages
                    if crate::protocol::history::citations::resource_annotations(item) =>
                {
                    // The encrypted item contains public Nexus handles. Resolve them
                    // against current ownership before sending any provider file ID.
                    let mut items = Value::Array(vec![item.take()]);
                    if super::files::bind_items(node, index, kid, &mut items)
                        .await?
                        .is_some_and(|v| v != target)
                    {
                        return Err(Failure(StatusCode::BAD_REQUEST, "file_deployment_mismatch"));
                    }
                    *item = items.as_array_mut().unwrap().pop().unwrap();
                    super::containers::bind_restored(node, index, kid, target, item).await?;
                }
                Endpoint::OpenaiResponses if item["type"] == "text" => {
                    for c in item["citations"].as_array_mut().into_iter().flatten() {
                        if let Some(fid) = c.get("file_id").filter(|v| !v.is_null()) {
                            let id =
                                crate::protocol::history::citations::file_handle(fid, "file_nx_")
                                    .map_err(|_| {
                                    Failure(StatusCode::BAD_REQUEST, "invalid_file_reference")
                                })?;
                            let rec = node
                                .db
                                .native_file(id, kid, index.revision, Endpoint::AnthropicMessages)
                                .await
                                .map_err(super::files::resource_error)?;
                            if (rec.model, rec.deployment) != target {
                                return Err(Failure(
                                    StatusCode::BAD_REQUEST,
                                    "file_deployment_mismatch",
                                ));
                            }
                            // Claude's document citation response includes file_id; the
                            // corresponding input citation accepts only its document index.
                            c.as_object_mut().unwrap().remove("file_id");
                        }
                    }
                }
                _ => {}
            }
        }
    }
    Ok(())
}
