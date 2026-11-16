import re
from decimal import Decimal

import httpx

from nexus.api import create_app
from nexus.infrastructure.metrics import ProcessMetrics, prometheus_text
from nexus.shared.errors import ServiceError

SAMPLE = re.compile(r'^[a-z_]+(\{method="[A-Z]+"\})? -?[0-9.e+-]+$')


def snapshot():
    process = ProcessMetrics()
    process.response("GET", 200, 0)
    process.response("POST", 503, 0)
    process.response("TRACE", 200, 0)
    process.connection(0.25)
    return {
        "process": {**process.snapshot(), "waiting_locks": 2, "pool_checked_out": 1},
        "billing": {
            "usage_backlog": 7,
            "payment_query_ms": Decimal("250.0"),
            "settlement_seconds": None,
            "conflicting_events": 0,
        },
        "publication": {
            "publication_backlog": 3,
            "publication_lag_seconds": 1.5,
            "auto_publish_failing": 1,
            "auto_publish_pending_seconds": None,
        },
    }


def test_prometheus_text_exports_known_series_with_bounded_labels():
    text = prometheus_text(snapshot())
    lines = text.splitlines()
    samples = [line for line in lines if not line.startswith("#")]
    assert all(SAMPLE.match(line) for line in samples), samples
    assert 'nexus_api_http_requests_total{method="GET"} 1' in samples
    assert 'nexus_api_http_errors_total{method="POST"} 1' in samples
    assert 'nexus_api_http_requests_total{method="OTHER"} 1' in samples
    assert "nexus_api_db_connection_acquisitions_total 1" in samples
    assert "nexus_api_db_connection_acquire_seconds_total 0.25" in samples
    assert "nexus_db_waiting_locks 2" in samples
    assert "nexus_billing_usage_backlog 7" in samples
    assert "nexus_billing_payment_query_seconds 0.25" in samples
    assert "nexus_billing_conflicting_events 0" in samples
    assert "nexus_publication_lag_seconds 1.5" in samples
    assert "nexus_publication_auto_failing 1" in samples
    assert not any(line.startswith("nexus_publication_auto_pending_seconds") for line in samples)
    # A database average without samples in the last 24 hours has no value.
    assert not any(line.startswith("nexus_billing_settlement_seconds") for line in samples)
    assert "# TYPE nexus_billing_settlement_seconds gauge" in lines
    assert "# TYPE nexus_api_http_requests_total counter" in lines
    names = [line.split()[2] for line in lines if line.startswith("# TYPE")]
    assert len(names) == len(set(names))
    assert text.endswith("\n")


def test_prometheus_text_tolerates_a_process_without_traffic():
    text = prometheus_text({"process": {"http": {}, "database": {}}})
    assert "nexus_api_db_connection_acquisitions_total 0" in text
    assert "method=" not in text


async def test_metrics_formats_share_platform_admin_access(settings_factory, completed_setup):
    app = completed_setup(create_app(settings_factory()))
    seen = []

    async def operational_metrics(token):
        seen.append(token)
        if token != "admin-session":
            raise ServiceError("forbidden", "Platform administrator required", 403)
        return snapshot()

    app.state.operational_metrics = operational_metrics
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        for path in ["/internal/metrics", "/internal/metrics/prometheus"]:
            assert (await client.get(path)).status_code == 401
            denied = await client.get(path, headers={"Authorization": "Bearer developer"})
            assert denied.status_code == 403
            assert "nexus_" not in denied.text
        admin = {"Authorization": "Bearer admin-session"}
        legacy = await client.get("/internal/metrics", headers=admin)
        assert legacy.status_code == 200
        assert legacy.headers["content-type"] == "application/json"
        assert legacy.json()["billing"]["usage_backlog"] == 7
        assert legacy.json()["billing"]["payment_query_ms"] == 250.0
        text = await client.get("/internal/metrics/prometheus", headers=admin)
        assert text.status_code == 200
        assert text.headers["content-type"] == "text/plain; version=0.0.4; charset=utf-8"
        assert text.headers["cache-control"] == "no-store"
        assert "nexus_billing_usage_backlog 7" in text.text
    assert seen == ["developer", "developer", "admin-session", "admin-session"]
