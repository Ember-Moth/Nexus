"""Upstream resource views and purge request validation; no services are started."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from nexus.modules.publication.application.ports import KeyOwner
from nexus.modules.publication.application.resources import request_view, resource_view
from nexus.modules.publication.application.schemas import (
    PurgeRequestCreate,
    PurgeRequestView,
    UpstreamResourceView,
)


def row(**values):
    base = {
        "kind": "file",
        "id": uuid4(),
        "api_key_id": uuid4(),
        "cluster_id": uuid4(),
        "deployment_id": uuid4(),
        "created_at": datetime(2026, 11, 15, 12, 0, tzinfo=UTC),
        "bound": True,
        "state": "retained",
        "purge_id": None,
        "purge_state": None,
        "reason": None,
        "attempts": None,
        "last_error": None,
        "purge_created_at": None,
        "next_attempt": None,
        "finished_at": None,
    }
    return base | values


def test_purge_request_targets_a_key_or_one_resource():
    kid, rid = uuid4(), uuid4()
    assert PurgeRequestCreate(api_key_id=kid).resource_id is None
    one = PurgeRequestCreate(api_key_id=kid, kind="conversation", resource_id=rid)
    assert (one.kind, one.resource_id) == ("conversation", rid)
    for body in (
        {"api_key_id": kid, "kind": "file"},
        {"api_key_id": kid, "resource_id": rid},
        {"api_key_id": kid, "kind": "secret", "resource_id": rid},
        {"api_key_id": kid, "organization_id": uuid4()},
    ):
        with pytest.raises(ValidationError):
            PurgeRequestCreate(**body)


def test_resource_view_shows_the_handle_owner_and_purge():
    owner = KeyOwner(uuid4(), uuid4())
    bound = row()
    view = UpstreamResourceView(**resource_view(bound, {bound["api_key_id"]: owner}))
    assert view.handle == f"file_nx_{bound['id']}" and view.purge is None
    assert (view.organization_id, view.project_id) == (owner.organization_id, owner.project_id)
    stamp = datetime(2026, 11, 15, 13, 0, tzinfo=UTC)
    orphan = row(
        kind="conversation",
        bound=False,
        state="pending",
        purge_id=uuid4(),
        purge_state="pending",
        reason="orphan",
        attempts=2,
        last_error="upstream_unavailable",
        purge_created_at=stamp,
        next_attempt=stamp,
    )
    view = UpstreamResourceView(**resource_view(orphan, {}))
    assert view.handle is None and view.organization_id is None
    assert (view.purge.state, view.purge.next_attempt_at) == ("pending", stamp)
    finished = row(
        purge_id=uuid4(),
        purge_state="abandoned",
        state="failed",
        reason="key_inactive",
        attempts=30,
        purge_created_at=stamp,
        next_attempt=stamp,
        finished_at=stamp,
    )
    view = UpstreamResourceView(**resource_view(finished, {}))
    assert (view.purge.state, view.purge.next_attempt_at) == ("failed", None)


@pytest.mark.parametrize(
    "counts, state",
    [
        ((2, 1, 1, 0), "pending"),
        ((2, 0, 1, 1), "failed"),
        ((2, 0, 2, 0), "purged"),
        ((0,) * 4, "purged"),
    ],
)
def test_request_state_summarizes_its_purges(counts, state):
    total, pending, purged, failed = counts
    view = PurgeRequestView(
        **request_view(
            {
                "id": uuid4(),
                "organization_id": uuid4(),
                "api_key_id": uuid4(),
                "kind": None,
                "resource_id": None,
                "actor_id": uuid4(),
                "created_at": datetime.now(UTC),
                "total": total,
                "pending": pending,
                "purged": purged,
                "failed": failed,
            }
        )
    )
    assert view.state == state
