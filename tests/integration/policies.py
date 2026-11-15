"""Change a policy the way an operator does: read it, then replace it with If-Match."""


def merged(current, update):
    result = dict(current)
    for name, value in update.items():
        result[name] = (
            merged(current[name], value)
            if isinstance(value, dict) and isinstance(current.get(name), dict)
            else value
        )
    return result


def body_of(view):
    """The PUT body of a policy view: everything but its version and lock."""
    return {k: v for k, v in view.items() if k not in {"version", "managed"}}


async def put_policy(client, token, path, update):
    headers = {"Authorization": f"Bearer {token}"}
    current = await client.get(path, headers=headers)
    assert current.status_code == 200, current.text
    body = merged(body_of(current.json()), update)
    written = await client.put(
        path, headers={**headers, "If-Match": current.headers["ETag"]}, json=body
    )
    assert written.status_code == 200, written.text
    assert written.headers["ETag"] == f'"{written.json()["version"]}"'
    return written.json()


async def put_overrides(client, token, cluster_id, update):
    """Merge ``update`` into a cluster's overrides; None values follow the platform defaults."""
    path = f"/api/v1/gateway-clusters/{cluster_id}/policy"
    headers = {"Authorization": f"Bearer {token}"}
    current = await client.get(path, headers=headers)
    assert current.status_code == 200, current.text
    body = merged(current.json()["overrides"], update)
    written = await client.put(
        path, headers={**headers, "If-Match": current.headers["ETag"]}, json=body
    )
    assert written.status_code == 200, written.text
    return written.json()
