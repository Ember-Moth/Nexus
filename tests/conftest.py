import base64
import os

import pytest

from nexus.config import RuntimeSettings


@pytest.fixture
def settings_factory():
    def create(**overrides):
        values = {
            "environment": "test",
            "database_url": "postgresql+psycopg://nexus_api:test-password@127.0.0.1:1/nexus_test",
            "redis_url": "redis://:test-password@127.0.0.1:2/0",
            "encryption_key": base64.urlsafe_b64encode(bytes(range(32))).decode(),
            "worker_id": "test-worker",
            "dependency_timeout": 0.2,
        }
        return RuntimeSettings(**(values | overrides))

    return create


def pytest_collection_modifyitems(items):
    if not os.environ.get("NEXUS_TEST_SERVICES"):
        skip = pytest.mark.skip(
            reason="run through scripts/run_integration.py for isolated services"
        )
        for item in items:
            if "integration" in item.keywords:
                item.add_marker(skip)
