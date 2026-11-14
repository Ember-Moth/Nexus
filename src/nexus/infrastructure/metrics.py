"""Bounded process-local observations supplemented by authoritative database gauges."""

from collections import Counter
from decimal import Decimal
from time import monotonic

# Prometheus text exposition format 0.0.4.
PROMETHEUS_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"

# Database gauges of the operational snapshot: section -> key -> (name, help).
# Averages cover the last 24 hours. Absent samples are omitted.
_GAUGES = {
    "process": {
        "waiting_locks": ("nexus_db_waiting_locks", "Ungranted locks in the database"),
        "pool_checked_out": ("nexus_api_db_pool_checked_out", "Connections checked out"),
    },
    "billing": {
        "usage_backlog": ("nexus_billing_usage_backlog", "Usage jobs ready to settle"),
        "waiting_for_facts": ("nexus_billing_usage_waiting_for_facts", "Usage jobs awaiting facts"),
        "usage_review": ("nexus_billing_usage_review", "Usage jobs held for review"),
        "overdue_holds": ("nexus_billing_overdue_holds", "Held reservations past expiry"),
        "conflicting_events": ("nexus_billing_conflicting_events", "Usage event conflicts"),
        "duplicate_events": ("nexus_billing_duplicate_events", "Duplicate usage events"),
        "consumption_cases": ("nexus_billing_consumption_cases_open", "Open consumption cases"),
        "payment_cases": ("nexus_billing_payment_cases_open", "Open payment cases"),
        "reconciliation_differences": (
            "nexus_billing_reconciliation_differences",
            "Account reconciliation differences",
        ),
        "payment_query_ms": ("nexus_billing_payment_query_seconds", "Payment query time"),
        "notice_receive_seconds": (
            "nexus_billing_notice_receive_seconds",
            "Payment notice delivery delay",
        ),
        "event_receive_seconds": (
            "nexus_billing_event_receive_seconds",
            "Usage event delivery delay",
        ),
        "settlement_seconds": (
            "nexus_billing_settlement_seconds",
            "Time from last usage event to settlement",
        ),
        "payment_confirmation_to_credit_seconds": (
            "nexus_billing_payment_confirmation_to_credit_seconds",
            "Time from payment confirmation to credit",
        ),
        "payment_posting_seconds": (
            "nexus_billing_payment_posting_seconds",
            "Time from payment observation to credit",
        ),
    },
    "publication": {
        "publication_backlog": ("nexus_publication_backlog", "Undistributed config releases"),
        "publication_lag_seconds": (
            "nexus_publication_lag_seconds",
            "Age of the oldest undistributed release",
        ),
        "lagging_nodes": ("nexus_publication_lagging_nodes", "Gateway nodes behind desired"),
    },
}
# Counters of this API process since it started; HTTP counters are labelled by method.
_HTTP = {
    "requests": ("nexus_api_http_requests_total", "HTTP requests handled"),
    "errors": ("nexus_api_http_errors_total", "HTTP responses with status 400 or above"),
    "seconds": ("nexus_api_http_request_seconds_total", "Time spent handling HTTP requests"),
}
_QUERIES = {
    "acquisitions": ("nexus_api_db_connection_acquisitions_total", "Connections acquired"),
    "acquire_seconds": ("nexus_api_db_connection_acquire_seconds_total", "Connection wait time"),
}


class ProcessMetrics:
    def __init__(self):
        self.http = Counter()
        self.queries = Counter()

    def response(self, method, status, started):
        label = method if method in {"GET", "POST", "PATCH", "PUT", "DELETE"} else "OTHER"
        self.http[label, "requests"] += 1
        self.http[label, "seconds"] += monotonic() - started
        if status >= 400:
            self.http[label, "errors"] += 1

    def connection(self, seconds):
        self.queries["acquisitions"] += 1
        self.queries["acquire_seconds"] += seconds

    def snapshot(self):
        return {
            "http": {
                method: {key: self.http[method, key] for key in ("requests", "errors", "seconds")}
                for method, _ in self.http
            },
            "database": dict(self.queries),
        }


def _number(value) -> str:
    if isinstance(value, bool):
        return str(int(value))
    if isinstance(value, int):
        return str(value)
    return repr(float(value))


def prometheus_text(snapshot) -> str:
    """Render the snapshot behind ``GET /internal/metrics`` as Prometheus text.

    Only the names above are exported. The only label is the bounded HTTP method.
    """
    lines = []

    def family(name, kind, description, samples):
        lines.extend((f"# HELP {name} {description}.", f"# TYPE {name} {kind}"))
        lines.extend(f"{name}{labels} {_number(value)}" for labels, value in samples)

    process = snapshot.get("process", {})
    http = sorted(process.get("http", {}).items())
    for key, (name, description) in _HTTP.items():
        family(name, "counter", description, [(f'{{method="{m}"}}', c[key]) for m, c in http])
    for key, (name, description) in _QUERIES.items():
        family(name, "counter", description, [("", process.get("database", {}).get(key, 0))])
    for section, gauges in _GAUGES.items():
        for key, (name, description) in gauges.items():
            value = snapshot.get(section, {}).get(key)
            if value is not None and key.endswith("_ms"):
                value = Decimal(str(value)) / 1000
            family(name, "gauge", description, [] if value is None else [("", value)])
    return "\n".join(lines) + "\n"
