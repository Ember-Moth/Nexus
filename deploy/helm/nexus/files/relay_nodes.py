"""Give relay ordinals 0..N-1 a database login and a registered gateway principal.

Run by the relay-node Job (deploy/helm/nexus/templates/hooks-relay-nodes.yaml) before and after
every install or upgrade, so `helm upgrade --set relay.replicas=N` is the whole "add a relay
node" procedure. For each ordinal it:

1. reads the Secret "<prefix>-<ordinal>" (username, password, node-id), or creates it with a
   random password. A node_id already registered for that login in this cluster is reused, so a
   re-created Secret does not orphan the old principal;
2. creates the PostgreSQL login (LOGIN, member of nexus_gateway only) or sets its password to the
   Secret's, so the Secret stays authoritative;
3. registers the principal with PUT /api/v1/gateway-clusters/{cluster}/principals/{node_id}.

Step 3 needs a running API. Before an install there is none, so --phase pre only warns; --phase
post waits for the API and fails if registration does not succeed. All steps are idempotent.
"""

import argparse
import base64
import json
import os
import secrets
import ssl
import sys
import time
import urllib.error
import urllib.request
import uuid

import httpx
import psycopg

from nexus.infrastructure.provisioning import ensure_gateway_login

ACCOUNT = "/var/run/secrets/kubernetes.io/serviceaccount"


def log(message: str) -> None:
    print(message, flush=True)


class Kubernetes:
    def __init__(self) -> None:
        host = os.environ["KUBERNETES_SERVICE_HOST"]
        if ":" in host:
            host = f"[{host}]"
        self.base = f"https://{host}:{os.environ['KUBERNETES_SERVICE_PORT']}"
        self.context = ssl.create_default_context(cafile=f"{ACCOUNT}/ca.crt")
        with open(f"{ACCOUNT}/token") as stream:
            self.token = stream.read().strip()
        with open(f"{ACCOUNT}/namespace") as stream:
            self.namespace = stream.read().strip()

    def request(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        request = urllib.request.Request(
            self.base + path,
            method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, context=self.context, timeout=15) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, {}

    def secret(self, name: str) -> dict[str, str] | None:
        status, body = self.request("GET", f"/api/v1/namespaces/{self.namespace}/secrets/{name}")
        if status == 404:
            return None
        if status != 200:
            raise SystemExit(f"cannot read Secret {name}: HTTP {status}")
        return {k: base64.b64decode(v).decode() for k, v in body.get("data", {}).items()}

    def create_secret(self, name: str, ordinal: int, data: dict[str, str]) -> None:
        body = {
            "apiVersion": "v1",
            "kind": "Secret",
            "type": "Opaque",
            "metadata": {
                "name": name,
                "labels": {
                    "app.kubernetes.io/part-of": "nexus",
                    "app.kubernetes.io/component": "relay",
                    "nexus.io/relay-ordinal": str(ordinal),
                },
                # Not owned by the Helm release: uninstalling the chart keeps node logins.
                "annotations": {"nexus.io/created-by": "relay-node-job"},
            },
            "data": {k: base64.b64encode(v.encode()).decode() for k, v in data.items()},
        }
        status, _ = self.request("POST", f"/api/v1/namespaces/{self.namespace}/secrets", body)
        if status not in (200, 201):
            raise SystemExit(f"cannot create Secret {name}: HTTP {status}")


def registered_node(connection: psycopg.Connection, cluster: str, role: str) -> str | None:
    exists = connection.execute("SELECT to_regclass('publication.gateway_principals')").fetchone()
    if exists[0] is None:
        return None
    row = connection.execute(
        "SELECT node_id::text FROM publication.gateway_principals "
        "WHERE cluster_id=%s AND database_role=%s",
        (cluster, role),
    ).fetchone()
    return row[0] if row else None


def credentials(kube, connection, args, ordinal: int) -> dict[str, str]:
    name = f"{args.prefix}-{ordinal}"
    data = kube.secret(name)
    if data is None:
        role = f"{args.role_prefix}{ordinal}"
        data = {
            "username": role,
            "password": secrets.token_urlsafe(32),
            "node-id": registered_node(connection, args.cluster, role) or str(uuid.uuid4()),
        }
        kube.create_secret(name, ordinal, data)
        log(f"node {ordinal}: created Secret {name}")
    missing = {"username", "password", "node-id"} - data.keys()
    if missing:
        raise SystemExit(f"Secret {name} lacks keys {sorted(missing)}")
    created = ensure_gateway_login(connection, data["username"], data["password"])
    log(f"node {ordinal}: login {data['username']} {'created' if created else 'up to date'}")
    return data


def register(api: httpx.Client, args, nodes: dict[int, dict[str, str]]) -> None:
    response = api.post(
        "/api/v1/auth/sessions",
        json={"login": os.environ["DEPLOYER_LOGIN"], "password": os.environ["DEPLOYER_PASSWORD"]},
    )
    if response.status_code != 201:
        raise SystemExit(f"deployer sign-in failed: HTTP {response.status_code}")
    headers = {"Authorization": f"Bearer {response.json()['access_token']}"}
    try:
        for ordinal, data in sorted(nodes.items()):
            response = api.put(
                f"/api/v1/gateway-clusters/{args.cluster}/principals/{data['node-id']}",
                headers=headers,
                json={
                    "node_id": data["node-id"],
                    "database_role": data["username"],
                    "capabilities": args.capabilities,
                    "active": True,
                },
            )
            if response.status_code != 200:
                raise SystemExit(
                    f"node {ordinal}: registration failed: HTTP {response.status_code} "
                    f"{response.text[:300]}"
                )
            log(f"node {ordinal}: registered as {data['node-id']}")
    finally:
        api.delete("/api/v1/auth/sessions/current", headers=headers)


def api_ready(api: httpx.Client) -> bool:
    try:
        return api.get("/health/ready").status_code == 200
    except httpx.HTTPError:
        return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=["pre", "post"], required=True)
    args = parser.parse_args()
    args.cluster = str(uuid.UUID(os.environ["RELAY_CLUSTER_ID"]))
    args.replicas = int(os.environ["RELAY_REPLICAS"])
    args.prefix = os.environ["RELAY_SECRET_PREFIX"]
    args.role_prefix = os.environ["RELAY_ROLE_PREFIX"]
    args.capabilities = json.loads(os.environ["RELAY_CAPABILITIES"])
    kube = Kubernetes()
    with psycopg.connect(os.environ["ADMIN_DATABASE_URL"], autocommit=True) as connection:
        connection.execute("SET search_path TO pg_catalog")
        nodes = {i: credentials(kube, connection, args, i) for i in range(args.replicas)}
    with httpx.Client(base_url=os.environ["API_URL"], timeout=15) as api:
        if args.phase == "pre" and not api_ready(api):
            # First install: the API starts after this hook; the post hook registers the nodes.
            log("API not ready yet; registration is left to the post-install hook")
            return 0
        deadline = time.monotonic() + 600
        while not api_ready(api):
            if time.monotonic() > deadline:
                raise SystemExit("API did not become ready within 10 minutes")
            time.sleep(5)
        register(api, args, nodes)
    return 0


if __name__ == "__main__":
    sys.exit(main())
