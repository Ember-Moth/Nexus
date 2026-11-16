"""End-to-end smoke test of the k3s deployment (docs/deployment.md#本机冒烟). Standard library only.

Deploys nexus-data and nexus from scratch into a k3s cluster prepared by setup-vm.sh, then:
  1. checks the migration and policy-import hooks;
  2. creates an administrator, an organization with funds, an upstream (a local TLS stand-in),
     a deployment with a relay profile, prices, routes and an API key;
  3. creates a gateway cluster, scales the relay to two nodes (the relay-node Job creates their
     logins and registers them) and publishes;
  4. calls the relay through Traefik over HTTP, SSE and WebSocket and reconciles the ledger;
  drills (--drills):
  A. a ConfigMap change rolls exactly the component that reads it;
  B. a relay rolling update under load: in-flight streams finish, no request fails, and the
     ledger balances.

kubectl and helm are taken from $KUBECTL and $HELM (default "kubectl", "helm"); both may be
command prefixes, e.g. KUBECTL="limactl shell nexus-k3s kubectl" when running on the macOS host.
"""

import argparse
import base64
import http.client
import json
import os
import pathlib
import re
import secrets
import shlex
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from decimal import Decimal

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
KUBECTL = shlex.split(os.environ.get("KUBECTL", "kubectl"))
HELM = shlex.split(os.environ.get("HELM", "helm"))
NS = "nexus"
UPSTREAM_NS = "nexus-upstream"
RELEASE = "nexus"
HOSTS = {"api": "api.nexus.test", "console": "console.nexus.test", "relay": "relay.nexus.test"}
RESULTS: list[tuple[str, str]] = []


def step(title: str) -> None:
    print(f"\n=== {title}", flush=True)


def ok(check: str, detail: str = "") -> None:
    RESULTS.append(("PASS", check))
    print(f"  PASS {check}" + (f"  ({detail})" if detail else ""), flush=True)


def fail(check: str, detail: str) -> None:
    RESULTS.append(("FAIL", check))
    print(f"  FAIL {check}: {detail}", flush=True)
    raise SystemExit(1)


def expect(condition, check: str, detail: str = "") -> None:
    if condition:
        ok(check, detail)
    else:
        fail(check, detail or "condition not met")


def run(command, *, input=None, check=True, capture=True, timeout=900) -> str:
    result = subprocess.run(
        command,
        input=input,
        text=True,
        capture_output=capture,
        timeout=timeout,
    )
    if check and result.returncode:
        print(result.stdout or "", result.stderr or "", file=sys.stderr)
        raise SystemExit(f"command failed: {' '.join(command[:6])} ...")
    return result.stdout if capture else ""


def kubectl(*args, **kwargs) -> str:
    return run([*KUBECTL, *args], **kwargs)


def helm(*args, **kwargs) -> str:
    return run([*HELM, *args], **kwargs)


def apply(document) -> None:
    kubectl("apply", "-f", "-", input=json.dumps(document))


def secret_exists(name: str, namespace: str = NS) -> bool:
    return bool(kubectl("-n", namespace, "get", "secret", name, "--ignore-not-found", "-o", "name"))


def secret_value(name: str, key: str, namespace: str = NS) -> str:
    raw = kubectl("-n", namespace, "get", "secret", name, "-o", f"jsonpath={{.data.{key}}}")
    return base64.b64decode(raw).decode()


def create_secret(name: str, data: dict, *, labels=None, kind="Opaque") -> None:
    if secret_exists(name):
        return
    apply(
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "type": kind,
            "metadata": {"name": name, "namespace": NS, "labels": labels or {}},
            "stringData": data,
        }
    )


# --- HTTPS through Traefik ---------------------------------------------------------------------


class Ingress:
    """Connects to the ingress address with the right SNI and Host, trusting the smoke CA."""

    def __init__(self, address: str, ca: str):
        self.address = address
        self.context = ssl.create_default_context(cafile=ca)

    def socket(self, host: str, timeout=30.0) -> ssl.SSLSocket:
        raw = socket.create_connection((self.address, 443), timeout=timeout)
        return self.context.wrap_socket(raw, server_hostname=host)

    def connection(self, host: str, timeout=30.0) -> http.client.HTTPSConnection:
        connection = http.client.HTTPSConnection(host, 443, context=self.context, timeout=timeout)
        connection.sock = self.socket(host, timeout)
        return connection

    def request(self, host, method, path, body=None, headers=None, timeout=30.0):
        connection = self.connection(host, timeout)
        payload = None if body is None else json.dumps(body).encode()
        merged = {"Accept": "application/json", **(headers or {})}
        if payload is not None:
            merged["Content-Type"] = "application/json"
        connection.request(method, path, body=payload, headers=merged)
        response = connection.getresponse()
        data = response.read()
        connection.close()
        return response.status, dict(response.getheaders()), data


class Api:
    def __init__(self, ingress: Ingress):
        self.ingress = ingress
        self.token = None

    def call(self, method, path, body=None, *, expect=None, version=None, idem=None):
        headers = {}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if version is not None:
            headers["If-Match"] = f'"{version}"'
        if idem:
            headers["Idempotency-Key"] = idem
        status, response_headers, data = self.ingress.request(
            HOSTS["api"], method, "/api/v1" + path, body, headers
        )
        if expect is not None and status not in (
            expect if isinstance(expect, tuple) else (expect,)
        ):
            fail(f"{method} {path}", f"HTTP {status}: {data[:400]!r}")
        return json.loads(data) if data else None


# --- Relay clients -------------------------------------------------------------------------------


def relay_http(ingress, key, model, text="ping", timeout=60.0) -> dict:
    status, _, data = ingress.request(
        HOSTS["relay"],
        "POST",
        "/v1/responses",
        {"model": model, "input": text, "max_output_tokens": 6},
        {"Authorization": f"Bearer {key}"},
        timeout,
    )
    if status != 200:
        raise RuntimeError(f"HTTP {status}: {data[:200]!r}")
    return json.loads(data)


def relay_sse(ingress, key, model, text="ping", timeout=120.0) -> list[dict]:
    connection = ingress.connection(HOSTS["relay"], timeout)
    connection.request(
        "POST",
        "/v1/responses",
        body=json.dumps({"model": model, "input": text, "max_output_tokens": 6, "stream": True}),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    response = connection.getresponse()
    if response.status != 200:
        raise RuntimeError(f"HTTP {response.status}: {response.read()[:200]!r}")
    events = []
    while True:
        line = response.readline()
        if not line:
            break
        if line.startswith(b"data: "):
            events.append(json.loads(line[6:]))
            if events[-1].get("type") in ("response.completed", "error"):
                break
    connection.close()
    if not events or events[-1].get("type") != "response.completed":
        raise RuntimeError(f"stream ended without response.completed: {events[-1:]}")
    return events


def _ws_frame(payload: bytes, opcode=0x1) -> bytes:
    mask = os.urandom(4)
    header = bytes([0x80 | opcode])
    if len(payload) < 126:
        header += bytes([0x80 | len(payload)])
    elif len(payload) < 65536:
        header += bytes([0x80 | 126]) + struct.pack("!H", len(payload))
    else:
        header += bytes([0x80 | 127]) + struct.pack("!Q", len(payload))
    return header + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload))


def _ws_read(stream):
    head = stream.read(2)
    if len(head) < 2:
        raise RuntimeError("websocket closed")
    opcode, length = head[0] & 0x0F, head[1] & 0x7F
    if length == 126:
        length = struct.unpack("!H", stream.read(2))[0]
    elif length == 127:
        length = struct.unpack("!Q", stream.read(8))[0]
    return opcode, stream.read(length)


def relay_ws(ingress, key, model, text="ping", timeout=120.0) -> list[dict]:
    sock = ingress.socket(HOSTS["relay"], timeout)
    nonce = base64.b64encode(os.urandom(16)).decode()
    sock.sendall(
        (
            f"GET /v1/responses HTTP/1.1\r\nHost: {HOSTS['relay']}\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {nonce}\r\nSec-WebSocket-Version: 13\r\n"
            f"Authorization: Bearer {key}\r\n\r\n"
        ).encode()
    )
    stream = sock.makefile("rb")
    status = stream.readline()
    while stream.readline() not in (b"\r\n", b""):
        pass
    if b" 101 " not in status:
        raise RuntimeError(f"upgrade refused: {status!r}")
    create = {"type": "response.create", "model": model, "input": text, "max_output_tokens": 6}
    sock.sendall(_ws_frame(json.dumps(create).encode()))
    events = []
    while True:
        opcode, payload = _ws_read(stream)
        if opcode == 0x8:
            break
        if opcode == 0x9:
            sock.sendall(_ws_frame(payload, 0xA))
            continue
        if opcode == 0x1:
            events.append(json.loads(payload))
            if events[-1].get("type") in ("response.completed", "error"):
                break
    try:
        sock.sendall(_ws_frame(struct.pack("!H", 1000), 0x8))
        sock.close()
    except OSError:
        pass
    if not events or events[-1].get("type") != "response.completed":
        raise RuntimeError(f"session ended without response.completed: {events[-1:]}")
    return events


# --- Steps ---------------------------------------------------------------------------------------


def stub_manifests(image: str) -> list[dict]:
    script = (HERE / "upstream_stub.py").read_text()
    labels = {"app": "upstream-stub"}
    return [
        {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": UPSTREAM_NS}},
        {
            "apiVersion": "cert-manager.io/v1",
            "kind": "Issuer",
            "metadata": {"name": "selfsigned", "namespace": UPSTREAM_NS},
            "spec": {"selfSigned": {}},
        },
        {
            "apiVersion": "cert-manager.io/v1",
            "kind": "Certificate",
            "metadata": {"name": "upstream-ca", "namespace": UPSTREAM_NS},
            "spec": {
                "isCA": True,
                "commonName": "Smoke upstream private CA",
                "secretName": "upstream-ca",
                "privateKey": {"algorithm": "ECDSA", "size": 256},
                "issuerRef": {"name": "selfsigned", "kind": "Issuer"},
            },
        },
        {
            "apiVersion": "cert-manager.io/v1",
            "kind": "Issuer",
            "metadata": {"name": "upstream-ca", "namespace": UPSTREAM_NS},
            "spec": {"ca": {"secretName": "upstream-ca"}},
        },
        {
            "apiVersion": "cert-manager.io/v1",
            "kind": "Certificate",
            "metadata": {"name": "upstream-stub", "namespace": UPSTREAM_NS},
            "spec": {
                "secretName": "upstream-stub-tls",
                "dnsNames": [f"upstream-stub.{UPSTREAM_NS}.svc"],
                "privateKey": {"algorithm": "ECDSA", "size": 256},
                "issuerRef": {"name": "upstream-ca", "kind": "Issuer"},
            },
        },
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "upstream-stub", "namespace": UPSTREAM_NS},
            "data": {"upstream_stub.py": script},
        },
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": "upstream-stub", "namespace": UPSTREAM_NS},
            "spec": {
                "replicas": 1,
                "selector": {"matchLabels": labels},
                "template": {
                    "metadata": {"labels": labels},
                    "spec": {
                        "securityContext": {"runAsNonRoot": True, "runAsUser": 65532},
                        "containers": [
                            {
                                "name": "stub",
                                "image": image,
                                "imagePullPolicy": "Never",
                                "command": ["python", "/opt/stub/upstream_stub.py"],
                                "env": [
                                    {"name": "TLS_CERT", "value": "/tls/tls.crt"},
                                    {"name": "TLS_KEY", "value": "/tls/tls.key"},
                                ],
                                "ports": [{"containerPort": 8443}, {"containerPort": 8080}],
                                "readinessProbe": {"tcpSocket": {"port": 8443}},
                                "volumeMounts": [
                                    {"name": "script", "mountPath": "/opt/stub"},
                                    {"name": "tls", "mountPath": "/tls"},
                                ],
                            }
                        ],
                        "volumes": [
                            {"name": "script", "configMap": {"name": "upstream-stub"}},
                            {"name": "tls", "secret": {"secretName": "upstream-stub-tls"}},
                        ],
                    },
                },
            },
        },
        {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": "upstream-stub", "namespace": UPSTREAM_NS},
            "spec": {
                "selector": labels,
                "ports": [
                    {"name": "https", "port": 8443, "targetPort": 8443},
                    {"name": "echo", "port": 8080, "targetPort": 8080},
                ],
            },
        },
        # Header echo behind Traefik, to see which client address the ingress passes on.
        {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "Ingress",
            "metadata": {
                "name": "echo",
                "namespace": UPSTREAM_NS,
                "annotations": {
                    "cert-manager.io/cluster-issuer": "smoke-ca",
                    "traefik.ingress.kubernetes.io/router.entrypoints": "websecure",
                    "traefik.ingress.kubernetes.io/router.tls": "true",
                },
            },
            "spec": {
                "ingressClassName": "traefik",
                "tls": [{"hosts": ["echo.nexus.test"], "secretName": "echo-tls"}],
                "rules": [
                    {
                        "host": "echo.nexus.test",
                        "http": {
                            "paths": [
                                {
                                    "path": "/",
                                    "pathType": "Prefix",
                                    "backend": {
                                        "service": {
                                            "name": "upstream-stub",
                                            "port": {"number": 8080},
                                        }
                                    },
                                }
                            ]
                        },
                    }
                ],
            },
        },
    ]


def prepare_secrets() -> dict:
    """Stand-in for External Secrets / SOPS: random values, created once, never printed."""
    step("Layer 1: Secrets (generated once; production uses External Secrets or SOPS)")
    for role in ("api", "worker", "migrator"):
        create_secret(
            f"nexus-db-{role}",
            {"username": f"nexus_{role}", "password": secrets.token_hex(32)},
            labels={"cnpg.io/reload": "true"},
            kind="kubernetes.io/basic-auth",
        )
    create_secret("nexus-redis", {"password": secrets.token_hex(32)})
    create_secret(
        "nexus-encryption",
        {
            "encryption-key": base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),
            "encryption-key-version": "1",
        },
    )
    create_secret("nexus-auth-proxy", {"secret": secrets.token_urlsafe(33)})
    create_secret(
        "nexus-relay-cluster", {"private-key": base64.b64encode(secrets.token_bytes(32)).decode()}
    )
    create_secret("nexus-deployer", {"login": "smoke-admin", "password": secrets.token_urlsafe(24)})
    expect(secret_exists("nexus-backup-r2"), "backup bucket credentials present")
    # The R2 stand-in has one key pair; production has a token per bucket.
    s3 = {k: secret_value("nexus-backup-r2", k) for k in ("ACCESS_KEY_ID", "ACCESS_SECRET_KEY")}
    create_secret("nexus-app-r2", s3)
    ok("Secrets present", "db x3, redis, encryption, auth-proxy, relay-cluster, deployer, R2 x2")
    return {
        "admin_login": secret_value("nexus-deployer", "login"),
        "admin_password": secret_value("nexus-deployer", "password"),
    }


def reset() -> None:
    """Start from empty namespaces; keeps the S3 credentials that setup-vm.sh created."""
    step("--fresh: removing the previous run")
    s3 = {k: secret_value("nexus-backup-r2", k) for k in ("ACCESS_KEY_ID", "ACCESS_SECRET_KEY")}
    for release in (RELEASE, "nexus-data"):
        helm("uninstall", release, "-n", NS, "--wait", check=False)
    kubectl("delete", "namespace", NS, UPSTREAM_NS, "--ignore-not-found", "--wait=true",
            "--timeout=300s")  # fmt: skip
    kubectl("create", "namespace", NS)
    create_secret("nexus-backup-r2", {**s3, "ACCESS_REGION": "auto"})


def install_data(address: str) -> None:
    step("nexus-data: CloudNativePG cluster (managed roles, WAL archiving) and TLS Redis")
    helm(
        "upgrade", "--install", "nexus-data", str(REPO / "deploy/helm/nexus-data"),
        "-n", NS, "-f", str(HERE / "values-data.yaml"),
        "--set", f"postgresql.backup.endpointURL=http://{address}:7070",
        # A new cluster needs an empty WAL archive: one path per namespace incarnation.
        "--set", f"postgresql.backup.prefix=postgres/smoke-{namespace_uid()}",
        "--wait", "--timeout", "10m",
    )  # fmt: skip
    kubectl("-n", NS, "wait", "--for=condition=Ready", "cluster/nexus-pg", "--timeout=600s")
    kubectl("-n", NS, "rollout", "status", "deploy/nexus-redis", "--timeout=300s")
    roles = psql("SELECT string_agg(rolname, ',' ORDER BY rolname) FROM pg_roles "
                 "WHERE rolname LIKE 'nexus_%'")  # fmt: skip
    expect("nexus_api" in roles and "nexus_gateway" in roles, "managed roles created", roles)
    policy = kubectl(
        "-n", NS, "exec", "deploy/nexus-redis", "--",
        "sh", "-c", "redis-cli --tls --cacert /tls/ca.crt -h 127.0.0.1 config get maxmemory-policy",
    )  # fmt: skip
    expect("noeviction" in policy, "Redis maxmemory-policy is noeviction")


def namespace_uid() -> str:
    return kubectl("get", "namespace", NS, "-o", "jsonpath={.metadata.uid}")[:8]


def psql(query: str, database="nexus") -> str:
    primary = kubectl(
        "-n", NS, "get", "pods", "-l", "cnpg.io/cluster=nexus-pg,cnpg.io/instanceRole=primary",
        "-o", "jsonpath={.items[0].metadata.name}",
    )  # fmt: skip
    return kubectl(
        "-n", NS, "exec", primary, "-c", "postgres", "--",
        "psql", "-tAX", "-d", database, "-c", query,
    ).strip()  # fmt: skip


# Values that depend on the cluster, filled in by main(): the R2 stand-in's endpoint.
CLUSTER_VALUES: list[str] = []


def app_values(extra=()) -> list[str]:
    return ["-f", str(HERE / "values-app.yaml"), *CLUSTER_VALUES, *extra]


def install_app(extra=(), wait=True) -> None:
    args = [
        "upgrade", "--install", RELEASE, str(REPO / "deploy/helm/nexus"),
        "-n", NS, *app_values(extra), "--timeout", "15m",
    ]  # fmt: skip
    if wait:
        args.append("--wait")
    helm(*args)


def job_log(name: str) -> str:
    return kubectl("-n", NS, "logs", f"job/{name}", "--tail=50", check=False)


def deploy_from_scratch(address, ingress, api):
    """Steps 1-3: install both charts, configure the catalog, scale the relay to two nodes."""
    admin = prepare_secrets()

    step("upstream stand-in (private CA, HTTPS + WebSocket)")
    for document in stub_manifests("docker.io/nexus/control-plane:smoke"):
        apply(document)
    kubectl("-n", UPSTREAM_NS, "wait", "--for=condition=Ready", "certificate/upstream-stub",
            "--timeout=120s")  # fmt: skip
    kubectl("-n", UPSTREAM_NS, "rollout", "status", "deploy/upstream-stub", "--timeout=180s")
    apply(
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "smoke-upstream-ca", "namespace": NS},
            "data": {"ca.crt": secret_value("upstream-ca", "ca\\.crt", UPSTREAM_NS)},
        }
    )
    ok("upstream stand-in serving", f"https://upstream-stub.{UPSTREAM_NS}.svc:8443")

    install_data(address)

    step("nexus: first install (relay.replicas=0); hooks provision, migrate, import policy")
    install_app()
    revision = psql("SELECT version_num FROM nexus_meta.alembic_version")
    head = re.search(
        r'^SCHEMA_REVISION = "(.+)"$',
        (REPO / "src/nexus/shared/storage.py").read_text(),
        re.M,
    ).group(1)
    expect(revision == head, "migration hook reached head", revision)
    expect("Database roles and grants" in job_log("nexus-provision-database"),
           "provision-database hook succeeded")  # fmt: skip
    imported = json.loads(job_log("nexus-policy-import").strip().splitlines()[-1])
    statuses = {name: group["status"] for name, group in imported["groups"].items()}
    expect(
        set(statuses) == {"identity.auth_policy", "reporting.export_policy"}
        and imported["groups"]["reporting.export_policy"]["managed"],
        "policy-import hook wrote the policy file (reporting.export_policy locked)",
        json.dumps(statuses),
    )
    configmaps = kubectl("-n", NS, "get", "configmap", "-l", "app.kubernetes.io/instance=nexus",
                         "-o", "jsonpath={.items[*].metadata.name}")  # fmt: skip
    expect(all(f"nexus-{c}" in configmaps for c in ("api", "worker", "web", "policy")),
           "per-component ConfigMaps", configmaps)  # fmt: skip

    step("setup wizard, deployer account, organization, catalog")
    # The migration created the built-in administrator with the published default account.
    default = {"login": "admin@example.com", "password": "password"}
    api.token = api.call("POST", "/auth/sessions", default, expect=201)["access_token"]
    expect(api.call("GET", "/setup", expect=200)["required"], "a new install waits for setup")
    api.call("GET", "/organizations", expect=409)
    ok("management API locked until the setup wizard is completed")
    api.call("POST", "/setup", {
        "site": {"site_name": "Nexus smoke", "console_url": f"https://{HOSTS['console']}",
                 "contact_email": None},
        "email": "smoke-operator@nexus.internal", "password": secrets.token_urlsafe(24),
    }, expect=200)  # fmt: skip
    status, _, _ = api.ingress.request(HOSTS["api"], "POST", "/api/v1/auth/sessions", default)
    expect(status == 401, "the default password stops working after setup", f"HTTP {status}")
    # The Relay registration Job signs in as a dedicated platform administrator.
    created = api.call("POST", "/users", {"login": admin["admin_login"]}, expect=201)
    api.call("POST", "/auth/password",
             {"token": created["setup_token"], "password": admin["admin_password"]},
             expect=204)  # fmt: skip
    deployer = created["user"]["id"]
    roles = api.call("GET", f"/users/{deployer}/platform-roles", expect=200)
    api.call("PATCH", f"/users/{deployer}/platform-roles",
             {"roles": ["platform_admin", "platform_finance"]}, version=roles["version"],
             expect=200)  # fmt: skip
    session = api.call("POST", "/auth/sessions",
                       {"login": admin["admin_login"], "password": admin["admin_password"]},
                       expect=201)  # fmt: skip
    api.token = session["access_token"]
    ok("setup completed through the API; deployer administrator signed in via the ingress")
    run_id = uuid.uuid4().hex[:6]
    org = api.call("POST", "/organizations", {"name": f"Smoke {run_id}"}, expect=201)
    deadline = time.monotonic() + 120
    while api.call("GET", f"/organizations/{org['id']}", expect=200)["status"] != "active":
        if time.monotonic() > deadline:
            fail("organization activated by the Worker", "still provisioning")
        time.sleep(2)
    ok("organization active (Worker provisioned the account)")
    receiver = api.call("POST", "/receiving-accounts",
                        {"name": "Smoke bank", "reference": run_id + "-bank", "currency": "USD"},
                        idem=uuid.uuid4().hex, expect=201)  # fmt: skip
    record = api.call(
        "POST", f"/organizations/{org['id']}/funding-records",
        {"receiving_account_id": receiver["id"], "external_reference": uuid.uuid4().hex,
         "amount": "100", "currency": "USD",
         "received_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 60)),
         "evidence": "smoke test funding"},
        idem=uuid.uuid4().hex, expect=201,
    )  # fmt: skip
    api.call("POST", f"/organizations/{org['id']}/funding-records/{record['id']}/post",
             idem=uuid.uuid4().hex, version=record["version"], expect=200)  # fmt: skip
    model = api.call("POST", "/models", {
        "public_name": f"smoke-model-{run_id}", "display_name": "Smoke model",
        "protocols": ["openai_compatible"], "max_input_tokens": 128000,
        "max_output_tokens": 8192, "capabilities": {"streaming": True, "tools": True},
    }, expect=201)  # fmt: skip
    upstream = api.call("POST", "/upstreams", {
        "name": f"Smoke upstream {run_id}", "protocol_family": "openai_compatible",
        "base_url": f"https://upstream-stub.{UPSTREAM_NS}.svc:8443/v1",
    }, expect=201)  # fmt: skip
    credential = api.call("POST", f"/upstreams/{upstream['id']}/credentials",
                          {"name": "smoke", "secret": "sk-smoke-" + secrets.token_hex(8)},
                          expect=201)  # fmt: skip
    deployment = api.call("POST", "/deployments", {
        "name": f"smoke-deployment-{run_id}", "model_id": model["id"],
        "upstream_id": upstream["id"],
        "credential_id": credential["id"], "upstream_model": "stub-model", "region": "local",
    }, expect=201)  # fmt: skip
    deployment = api.call("PATCH", f"/deployments/{deployment['id']}", {"relay_profile": {
        "endpoint": "openai_responses", "transports": ["http", "websocket"],
        "input_ceiling": 64, "output_ceiling": 16, "streaming": True, "tools": True,
        "continuation": False, "cancellation": False,
        "bound_reference": "smoke-stub", "count_rule": "deployment_ceiling.v1",
    }}, version=deployment["version"], expect=200)  # fmt: skip
    for kind, target, price in (("sale", model, "0.01"), ("cost", deployment, "0.004")):
        body = {"currency": "USD", "rates": {
            "input_uncached_tokens": {"unit_price": price, "unit_quantity": 1},
            "output_tokens": {"unit_price": price, "unit_quantity": 1}}}  # fmt: skip
        body["model_id" if kind == "sale" else "deployment_id"] = target["id"]
        draft = api.call("POST", f"/{kind}-price-versions", body, idem=uuid.uuid4().hex,
                         expect=201)  # fmt: skip
        api.call("POST", f"/{kind}-price-versions/{draft['id']}/publish", {},
                 idem=uuid.uuid4().hex, version=draft["version"], expect=200)  # fmt: skip
    api.call("POST", f"/organizations/{org['id']}/access-policies", {
        "layer": "platform", "scope_type": "organization", "scope_id": None,
        "allowed_models": [model["id"]]}, expect=201)  # fmt: skip
    project = api.call("POST", f"/organizations/{org['id']}/projects", {"name": "smoke"},
                       expect=201)  # fmt: skip
    account = api.call(
        "POST",
        f"/organizations/{org['id']}/service-accounts",
        {"project_id": project["id"], "name": "smoke-app"},
        expect=201,
    )
    key = api.call("POST", f"/organizations/{org['id']}/api-keys",
                   {"service_account_id": account["id"], "name": "smoke"},
                   idem=uuid.uuid4().hex, expect=(200, 201))["secret"]  # fmt: skip
    api.call("PUT", f"/route-policies/{org['id']}/{model['id']}",
             {"candidates": [{"deployment_id": deployment["id"]}]}, expect=201)  # fmt: skip
    ok("organization funded, model/upstream/deployment/prices/route/key configured")

    step("gateway cluster, two relay nodes, publish")
    public_key = kubectl(
        "-n", NS, "exec", "-i", "deploy/nexus-api", "--", "python", "-c",
        "import base64,sys;from nacl.public import PrivateKey;"
        "print(base64.b64encode(bytes(PrivateKey(base64.b64decode(sys.stdin.read()))"
        ".public_key)).decode())",
        input=secret_value("nexus-relay-cluster", "private-key"),
    ).strip()  # fmt: skip
    cluster = api.call("POST", "/gateway-clusters", {
        "name": f"smoke-{run_id}", "region": "local", "organization_ids": [org["id"]],
        "deployment_ids": [deployment["id"]], "public_key": public_key, "auto_publish": True,
    }, expect=201)  # fmt: skip
    relay_set = ["--set", f"relay.clusterId={cluster['id']}", "--set", "relay.replicas=2"]
    install_app(relay_set)
    principals = api.call("GET", f"/gateway-clusters/{cluster['id']}/principals", expect=200)
    expect(len(principals["items"]) == 2, "two principals registered by the relay-node Job",
           ", ".join(p["database_role"] for p in principals["items"]))  # fmt: skip
    for node in principals["items"]:
        login = psql("SELECT rolcanlogin AND rolinherit FROM pg_roles "
                     f"WHERE rolname='{node['database_role']}'")  # fmt: skip
        member = psql("SELECT string_agg(g.rolname, ',') FROM pg_auth_members m "
                      "JOIN pg_roles g ON g.oid=m.roleid JOIN pg_roles r ON r.oid=m.member "
                      f"WHERE r.rolname='{node['database_role']}'")  # fmt: skip
        expect(
            login == "t" and member == "nexus_gateway",
            f"{node['database_role']} is a LOGIN that only inherits nexus_gateway",
        )
    release = api.call("POST", f"/gateway-clusters/{cluster['id']}/releases", {},
                       idem=uuid.uuid4().hex, expect=202)  # fmt: skip
    deadline = time.monotonic() + 180
    while True:
        nodes = api.call("GET", f"/gateway-clusters/{cluster['id']}/principals", expect=200)
        applied = [int(n["applied_revision"] or 0) for n in nodes["items"]]
        if all(a >= int(release["revision"]) for a in applied):
            break
        if time.monotonic() > deadline:
            fail("both nodes applied the release", f"applied {applied}")
        time.sleep(3)
    ok("published; both nodes applied the revision", f"revision {release['revision']}")
    kubectl("-n", NS, "rollout", "status", "statefulset/nexus-relay", "--timeout=300s")
    return org, model, key, relay_set


def attach(api, ingress, roll=True):
    """--reuse: continue with the organization, model and relay cluster a previous run created."""
    step("--reuse: attaching to the existing deployment")
    login, password = (
        secret_value("nexus-deployer", "login"),
        secret_value("nexus-deployer", "password"),
    )
    api.token = api.call("POST", "/auth/sessions", {"login": login, "password": password},
                         expect=201)["access_token"]  # fmt: skip
    values = json.loads(helm("get", "values", RELEASE, "-n", NS, "-a", "-o", "json"))
    cluster = values["relay"]["clusterId"]
    relay_set = ["--set", f"relay.clusterId={cluster}", "--set",
                 f"relay.replicas={values['relay']['replicas']}"]  # fmt: skip
    orgs = api.call("GET", "/organizations?limit=100", expect=200)["items"]
    org = [o for o in orgs if o["name"].startswith("Smoke ")][-1]
    models = api.call("GET", "/models?limit=100", expect=200)["items"]
    model = [m for m in models if m["public_name"].startswith("smoke-model-")][-1]
    account = api.call("GET", f"/organizations/{org['id']}/service-accounts", expect=200)
    key = api.call("POST", f"/organizations/{org['id']}/api-keys",
                   {"service_account_id": account["items"][0]["id"], "name": "smoke-reuse"},
                   idem=uuid.uuid4().hex, expect=(200, 201))["secret"]  # fmt: skip
    # The chart is re-applied unchanged (restores anything edited by hand), then the relay rolls
    # onto the current image; a new key reaches the nodes with the next automatic release.
    if roll:
        install_app(relay_set)
        kubectl("-n", NS, "rollout", "restart", "statefulset/nexus-relay")
        kubectl("-n", NS, "rollout", "status", "statefulset/nexus-relay", "--timeout=600s")
    deadline = time.monotonic() + 60
    while True:
        try:
            relay_http(ingress, key, model["public_name"])
            break
        except RuntimeError:
            if time.monotonic() > deadline:
                raise
            time.sleep(3)
    ok("attached", f"cluster {cluster}, organization {org['name']}")
    return org, model, key, relay_set


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ingress", help="address Traefik listens on (default: first node IP)")
    parser.add_argument("--drills", action="store_true", help="also run the two drills")
    parser.add_argument("--fresh", action="store_true", help="delete earlier runs first")
    parser.add_argument("--reuse", action="store_true",
                        help="skip installation and use what an earlier run deployed")  # fmt: skip
    parser.add_argument("--skip-roll", action="store_true",
                        help="with --reuse: keep the running relay pods")  # fmt: skip
    parser.add_argument("--long-stream", type=int, default=65,
                        help="seconds of one SSE stream and one WS turn (0 skips)")  # fmt: skip
    args = parser.parse_args()

    address = args.ingress or kubectl(
        "get",
        "nodes",
        "-o",
        "jsonpath={.items[0].status.addresses[?(@.type=='InternalIP')].address}",
    )
    step(f"cluster reachable; Traefik at {address}")
    CLUSTER_VALUES.extend(["--set", f"objectStorage.endpointURL=http://{address}:7070"])
    if args.fresh:
        reset()
    kubectl("create", "namespace", NS, check=False)
    ca = tempfile.NamedTemporaryFile("w", suffix=".crt", delete=False)
    ca.write(secret_value("smoke-root-ca", "ca\\.crt", "cert-manager"))
    ca.close()
    ingress = Ingress(address, ca.name)
    api = Api(ingress)
    if args.reuse:
        org, model, key, relay_set = attach(api, ingress, roll=not args.skip_roll)
    else:
        org, model, key, relay_set = deploy_from_scratch(address, ingress, api)

    step("calls through Traefik: HTTP, SSE, WebSocket")
    before = api.call("GET", f"/organizations/{org['id']}/account", expect=200)
    before["_calls"] = len(every(api, f"/organizations/{org['id']}/requests"))
    reply = relay_http(ingress, key, model["public_name"])
    expect(reply["status"] == "completed", "HTTP JSON /v1/responses", reply["id"])
    events = relay_sse(ingress, key, model["public_name"])
    expect(events[-1]["type"] == "response.completed", "SSE /v1/responses",
           f"{len(events)} events")  # fmt: skip
    events = relay_ws(ingress, key, model["public_name"])
    expect(events[-1]["type"] == "response.completed", "WebSocket /v1/responses",
           f"{len(events)} events")  # fmt: skip
    calls = 3
    if args.long_stream:
        hold = f"hold={args.long_stream}"
        started = time.monotonic()
        relay_sse(ingress, key, model["public_name"], hold, timeout=args.long_stream + 60)
        relay_ws(ingress, key, model["public_name"], hold, timeout=args.long_stream + 60)
        expect(
            time.monotonic() - started >= 2 * args.long_stream * 0.9,
            f"{args.long_stream}s SSE stream and WS turn survive the ingress timeouts",
        )
        calls += 2
    reconcile(api, org, before, calls, "ledger after the smoke calls")

    status, _, _ = ingress.request(HOSTS["relay"], "GET", "/ready")
    expect(status == 404, "relay /ready and /live are not routed by the ingress", f"HTTP {status}")
    status, _, _ = ingress.request(HOSTS["api"], "GET", "/internal/metrics")
    expect(status == 404, "API /internal is not routed by the ingress", f"HTTP {status}")
    status, _, _ = ingress.request(HOSTS["console"], "GET", "/login")
    expect(status == 200, "console /login through the ingress")
    client_ip(ingress, address)

    if args.drills:
        drill_configmap(relay_set)
        drill_relay_roll(api, ingress, key, model, org, relay_set)
    backups()
    print("\nSUMMARY")
    for verdict, check in RESULTS:
        print(f"  {verdict} {check}")
    return 0


def every(api, path: str) -> list[dict]:
    """All items of a cursor-paginated collection."""
    items, cursor = [], None
    while True:
        query = "?limit=100" + (f"&cursor={cursor}" if cursor else "")
        page = api.call("GET", path + query, expect=200)
        items += page["items"]
        cursor = page.get("next_cursor")
        if not cursor:
            return items


def reconcile(api, org, before, calls, title, timeout=180) -> None:
    """Every call settled exactly once; balance, holds and ledger agree."""
    deadline = time.monotonic() + timeout
    while True:
        items = every(api, f"/organizations/{org['id']}/requests")
        settled = [r for r in items if r["charged_amount"] is not None and r["settled_at"]]
        account = api.call("GET", f"/organizations/{org['id']}/account", expect=200)
        if len(settled) == len(items) and Decimal(account["held_amount"]) == 0:
            break
        if time.monotonic() > deadline:
            states = sorted({(r["state"], r["reservation_state"]) for r in items})
            fail(title, f"{len(settled)}/{len(items)} settled, held {account['held_amount']}, "
                        f"states {states}")  # fmt: skip
        time.sleep(3)
    charged = sum(Decimal(r["charged_amount"]) for r in items)
    ledger = every(api, f"/organizations/{org['id']}/ledger")
    consumption = [e for e in ledger if Decimal(e["signed_amount"]) < 0]
    latest = max(ledger, key=lambda e: e["sequence"])
    expect(len(items) == calls + int(before.get("_calls", 0)),
           f"{title}: one request per call", f"{len(items)} requests")  # fmt: skip
    expect(Decimal(account["balance"]) == Decimal("100") - charged
           and Decimal(latest["balance_after"]) == Decimal(account["balance"])
           and -sum(Decimal(e["signed_amount"]) for e in consumption) == charged,
           f"{title}: balance = funding - charges = last ledger balance, nothing held",
           f"balance {account['balance']}, charged {charged}, "
           f"{len(consumption)} debits")  # fmt: skip
    before["_calls"] = len(items)


def client_ip(ingress, address) -> None:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.connect((address, 443))
    mine = probe.getsockname()[0]
    probe.close()
    status, _, data = ingress.request("echo.nexus.test", "GET", "/headers",
                                      headers={"X-Real-Ip": "203.0.113.9",
                                               "X-Forwarded-For": "203.0.113.9"})  # fmt: skip
    seen = json.loads(data)["headers"]
    expect(
        status == 200 and seen.get("x-real-ip") == mine,
        "Traefik passes the real client address (forged X-Real-Ip replaced)",
        f"client {mine}, X-Real-Ip {seen.get('x-real-ip')}, XFF {seen.get('x-forwarded-for')}",
    )


def pods(component: str) -> dict[str, str]:
    out = kubectl(
        "-n",
        NS,
        "get",
        "pods",
        "-l",
        f"app.kubernetes.io/component={component}",
        "-o",
        'jsonpath={range .items[*]}{.metadata.name}={.metadata.uid}{"\\n"}{end}',
    )
    return dict(line.split("=", 1) for line in out.split() if "=" in line)


def drill_configmap(relay_set) -> None:
    step("drill A: a ConfigMap change rolls exactly the component that reads it")
    before = {c: pods(c) for c in ("api", "worker", "web", "relay")}
    install_app([*relay_set, "--set", "config.api.NEXUS_HASH_QUEUE_TIMEOUT_SECONDS=6"])
    kubectl("-n", NS, "rollout", "status", "deploy/nexus-api", "--timeout=300s")
    after = {c: pods(c) for c in ("api", "worker", "web", "relay")}
    expect(not set(before["api"].values()) & set(after["api"].values()),
           "API pods replaced after config.api changed")  # fmt: skip
    expect(all(before[c] == after[c] for c in ("worker", "web", "relay")),
           "Worker, Web and relay pods untouched")  # fmt: skip
    value = kubectl("-n", NS, "exec", "deploy/nexus-api", "--", "printenv",
                    "NEXUS_HASH_QUEUE_TIMEOUT_SECONDS").strip()  # fmt: skip
    expect(value == "6", "new API pods read the new value through envFrom", value)


def drill_relay_roll(api, ingress, key, model, org, relay_set) -> None:
    step("drill B: relay rolling update under load (drain, no lost requests, ledger)")
    before = api.call("GET", f"/organizations/{org['id']}/account", expect=200)
    before["_calls"] = len(every(api, f"/organizations/{org['id']}/requests"))  # fmt: skip
    outcomes: list[tuple[str, str]] = []
    lock = threading.Lock()
    stop = threading.Event()

    def record(kind, function, *call_args):
        try:
            function(ingress, key, model["public_name"], *call_args)
            result = "ok"
        except Exception as error:  # noqa: BLE001 - every failure is reported
            result = f"error: {error}"
        with lock:
            outcomes.append((kind, result))

    def long_streams():
        n = 0
        while not stop.is_set():
            kind, function = (("sse", relay_sse), ("ws", relay_ws))[n % 2]
            threading.Thread(target=record, args=(kind, function, "hold=12", 90)).start()
            n += 1
            time.sleep(3)

    def short_calls():
        while not stop.is_set():
            record("http", relay_http, "ping", 30)
            time.sleep(1)

    old = pods("relay")
    threads = [threading.Thread(target=long_streams), threading.Thread(target=short_calls)]
    for thread in threads:
        thread.start()
    time.sleep(8)
    started = time.monotonic()
    # A relay ConfigMap change: the checksum rolls the StatefulSet one pod at a time.
    install_app([*relay_set, "--set", "config.relay.NEXUS_RELAY_LOG=info\\,nexus_relay=info"],
                wait=False)  # fmt: skip
    kubectl("-n", NS, "rollout", "status", "statefulset/nexus-relay", "--timeout=600s")
    rolled = time.monotonic() - started
    time.sleep(10)
    stop.set()
    for thread in threads:
        thread.join()
    deadline = time.monotonic() + 150
    while threading.active_count() > 1 and time.monotonic() < deadline:
        time.sleep(1)
    new = pods("relay")
    expect(not set(old.values()) & set(new.values()), "both relay pods replaced",
           f"rolled in {rolled:.0f}s")  # fmt: skip
    errors = [o for o in outcomes if o[1] != "ok"]
    counts = {k: sum(1 for kind, _ in outcomes if kind == k) for k in ("http", "sse", "ws")}
    expect(not errors, "no request failed during the roll",
           f"{counts}; errors: {errors[:3]}")  # fmt: skip
    drained = kubectl("-n", NS, "logs", "-l", "app.kubernetes.io/component=relay",
                      "--previous", "--tail=200", check=False)  # fmt: skip
    if "drain finished" in drained:
        ok("relay logged a completed drain before exiting")
    reconcile(api, org, before, len(outcomes), "ledger after the rolling update", timeout=300)


def backups() -> None:
    step("backups: WAL archiving and base backup to the R2 stand-in")
    conditions = json.loads(kubectl("-n", NS, "get", "cluster/nexus-pg", "-o",
                                    "jsonpath={.status.conditions}"))  # fmt: skip
    archiving = next((c for c in conditions if c["type"] == "ContinuousArchiving"), {})
    detail = archiving.get("message", "")
    expect(archiving.get("status") == "True", "WAL archiving to object storage", detail)
    deadline = time.monotonic() + 300
    while True:
        phases = kubectl("-n", NS, "get", "backups.postgresql.cnpg.io", "-o",
                         "jsonpath={.items[*].status.phase}").split()  # fmt: skip
        if "completed" in phases:
            break
        if time.monotonic() > deadline or "failed" in phases:
            fail("base backup completed", f"phases {phases}")
        time.sleep(5)
    ok("base backup completed", ",".join(phases))


if __name__ == "__main__":
    sys.exit(main())
