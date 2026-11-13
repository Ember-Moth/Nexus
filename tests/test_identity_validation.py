import base64
import json

import pytest

from nexus.modules.identity import api


@pytest.mark.parametrize(
    "value",
    [
        ["2026-11-13T00:00:00Z", 123],
        [123, "00000000-0000-4000-8000-000000000001"],
        ["2026-11-13T00:00:00", "00000000-0000-4000-8000-000000000001"],
        {"position": 0},
    ],
)
def test_malformed_cursor_is_a_client_error(value):
    cursor = base64.urlsafe_b64encode(json.dumps(value).encode()).decode()
    with pytest.raises(api.IdentityError) as raised:
        api.cursor_value(cursor)
    assert raised.value.status == 422
