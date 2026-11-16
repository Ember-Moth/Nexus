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
            "object_storage_endpoint_url": "http://127.0.0.1:3",
            "object_storage_bucket": "nexus-app",
            "object_storage_region": "us-east-1",
            "object_storage_access_key_id": "test-access-key",
            "object_storage_secret_access_key": "test-object-secret",
        }
        return RuntimeSettings(**(values | overrides))

    return create


def pytest_addoption(parser):
    parser.addoption("--capacity", action="store_true", help="run timed settlement load profiles")


def pytest_collection_modifyitems(config, items):
    for item in items:
        if "capacity" in item.keywords and not config.getoption("--capacity"):
            item.add_marker(pytest.mark.skip(reason="select --capacity for the timed load run"))
    if not os.environ.get("NEXUS_TEST_SERVICES"):
        skip = pytest.mark.skip(
            reason="run through scripts/run_integration.py for isolated services"
        )
        for item in items:
            if "integration" in item.keywords:
                item.add_marker(skip)
