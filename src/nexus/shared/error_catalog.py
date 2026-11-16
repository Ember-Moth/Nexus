"""Published management error codes and client recovery guidance.

Groups share an HTTP status and action. Writes with an unknown outcome must
use their original idempotency key, or reconcile state before retrying.
"""

from typing import Literal

from pydantic import BaseModel


class ErrorGuidance(BaseModel):
    status: int
    action: str
    retry: Literal["after_correction", "backoff_if_safe", "reconcile_first"]


# Keyed by status as well as code because configuration and runtime calls
# may report the same condition with different HTTP semantics.
GROUPS = (
    (401, "authenticate", "after_correction", ("unauthorized",)),
    (
        403,
        "check_access",
        "after_correction",
        (
            "forbidden",
            "invalid_funding_actor",
            "policy_expands_access",
        ),
    ),
    (403, "contact_administrator", "after_correction", ("registration_unavailable",)),
    (
        403,
        "complete_challenge",
        "after_correction",
        (
            "turnstile_rejected",
            "turnstile_required",
        ),
    ),
    (
        404,
        "check_resource",
        "after_correction",
        (
            "invitation_unavailable",
            "not_found",
        ),
    ),
    (404, "check_route", "after_correction", ("route_not_found",)),
    (405, "check_method", "after_correction", ("method_not_allowed",)),
    (409, "adjust_budget", "after_correction", ("budget_exceeded",)),
    (409, "check_configuration", "after_correction", ("credential_not_ready",)),
    (409, "check_idempotency_key", "after_correction", ("idempotency_conflict",)),
    (
        409,
        "check_resource_state",
        "after_correction",
        (
            "account_conflict",
            "account_not_ready",
            "adjustment_exceeds_source",
            "already_member",
            "amount_overflow",
            "attempt_conflict",
            "billing_conflict",
            "case_conflict",
            "catalog_conflict",
            "cluster_changed",
            "conflict",
            "cost_conflict",
            "cost_unavailable",
            "deployment_protocol_conflict",
            "deployment_unavailable",
            "disable_channel_first",
            "event_conflict",
            "export_unavailable",
            "funding_binding_conflict",
            "gateway_funding_immutable",
            "invalid_adjustment_state",
            "invalid_attempt_sequence",
            "invalid_funding_state",
            "invalid_grant_target",
            "invalid_transition",
            "invitation_consumed",
            "key_not_rotatable",
            "member_inactive",
            "model_unavailable",
            "organization_archived",
            "organization_inactive",
            "owner_inactive",
            "parent_inactive",
            "payment_channel_disabled",
            "price_target_inactive",
            "price_unavailable",
            "principal_conflict",
            "project_archived",
            "project_inactive",
            "protocol_mismatch",
            "publication_conflict",
            "receiving_unavailable",
            "record_immutable",
            "request_conflict",
            "request_not_executable",
            "resource_archived",
            "rollback_scope_changed",
            "rollback_target_unavailable",
            "service_account_inactive",
            "settlement_conflict",
            "setup_completed",
            "source_changed",
            "unsupported_capability",
            "upstream_must_be_disabled",
            "user_disabled",
            "user_inactive",
            "verification_window_full",
            "version_immutable",
        ),
    ),
    (409, "choose_another_email", "after_correction", ("email_in_use",)),
    (409, "complete_setup", "after_correction", ("setup_required",)),
    (409, "choose_another_login", "after_correction", ("login_in_use",)),
    (409, "configure_provider", "after_correction", ("turnstile_not_configured",)),
    (409, "edit_policy_file", "after_correction", ("policy_managed_externally",)),
    (409, "keep_billing_currency", "after_correction", ("currency_locked",)),
    (409, "contact_administrator", "after_correction", ("account_frozen",)),
    (409, "fund_account", "after_correction", ("insufficient_funds",)),
    (409, "keep_personal_organization", "after_correction", ("personal_organization_required",)),
    (409, "keep_personal_owner", "after_correction", ("personal_owner_required",)),
    (409, "reload_auth_options", "after_correction", ("auth_policy_changed",)),
    (409, "retain_an_administrator", "after_correction", ("last_administrator",)),
    (409, "retain_an_owner", "after_correction", ("last_owner",)),
    (410, "request_new_invitation", "after_correction", ("invitation_expired",)),
    (412, "reload_resource", "after_correction", ("version_conflict",)),
    (
        422,
        "correct_input",
        "after_correction",
        (
            "currency_mismatch",
            "duplicate_candidate",
            "email_reserved",
            "email_verification_required",
            "gateway_binding_mismatch",
            "gateway_environment_mismatch",
            "gateway_unavailable",
            "invalid_adjustment_source",
            "invalid_archive",
            "invalid_bounds",
            "invalid_catalog_config",
            "invalid_cluster_policy",
            "invalid_cost_usage",
            "invalid_credential",
            "invalid_cursor",
            "invalid_deployment",
            "invalid_effective_at",
            "invalid_email",
            "invalid_evidence",
            "invalid_expiry",
            "invalid_gateway_configuration",
            "invalid_gateway_role",
            "invalid_idempotency_key",
            "invalid_login",
            "invalid_models",
            "invalid_password",
            "invalid_payment_bounds",
            "invalid_period",
            "invalid_received_at",
            "invalid_region",
            "invalid_reserve_amount",
            "invalid_route",
            "invalid_scope",
            "invalid_snapshot",
            "invalid_usage",
            "invalid_verification_token",
            "invalid_version",
            "node_mismatch",
            "organization_required",
            "payment_amount_outside_limits",
            "payment_precision",
            "price_dimensions_mismatch",
            "query_required",
            "registration_requires_email_verification",
            "relay_profile_required",
            "unsupported_event_version",
            "unsupported_payment_currency",
            "validation_error",
        ),
    ),
    (428, "supply_precondition", "after_correction", ("precondition_required",)),
    (
        429,
        "back_off",
        "backoff_if_safe",
        (
            "export_rate_limited",
            "rate_limited",
        ),
    ),
    (500, "contact_support", "reconcile_first", ("internal_error",)),
    (
        503,
        "check_configuration",
        "after_correction",
        (
            "credential_unavailable",
            "currency_config_mismatch",
            "payment_configuration_unavailable",
        ),
    ),
    (
        503,
        "check_dependency",
        "backoff_if_safe",
        (
            "authentication_unavailable",
            "payment_provider_unavailable",
            "storage_unavailable",
            "turnstile_unavailable",
        ),
    ),
    (
        503,
        "configure_provider",
        "after_correction",
        (
            "mail_unavailable",
            "payment_credentials_revoked",
            "payment_endpoints_unconfigured",
        ),
    ),
)
CATALOG = {
    (code, status): ErrorGuidance(status=status, action=action, retry=retry)
    for status, action, retry, codes in GROUPS
    for code in codes
}


def error_catalog() -> dict[str, list[ErrorGuidance]]:
    result = {}
    for (code, _), guidance in CATALOG.items():
        result.setdefault(code, []).append(guidance)
    return result
