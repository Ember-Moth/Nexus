"""Volatile loopback-only payment simulator. Never use with real money or production data."""

import argparse
import asyncio
import copy
import hashlib
import hmac
import json
import os
from datetime import UTC, datetime
from urllib.parse import urlencode
from uuid import uuid4


class MockPaymentProvider:
    def __init__(self, keys):
        # key -> stable (merchant, environment); credentials are never written to logs.
        self.keys = dict(keys)
        self.orders = {}
        self.objects = {}
        self.idempotency = {}
        self.create_calls = 0
        self.query_calls = 0
        self.used_keys = []
        self.drop_create_responses = 0
        self.query_override = None
        self.create_started = asyncio.Event()
        self.query_started = asyncio.Event()
        self.create_barrier = None
        self.query_barrier = None
        self.server = None

    async def start(self, port=0):
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", port)
        self.url = "http://127.0.0.1:" + str(self.server.sockets[0].getsockname()[1])
        return self

    async def close(self):
        self.server.close()
        await self.server.wait_closed()

    def _create(self, payload, merchant, environment):
        if payload["stable_merchant_id"] != merchant or payload["environment"] != environment:
            raise ValueError("wrong merchant binding")
        ref = payload["merchant_order_ref"]
        key = (merchant, environment, payload["idempotency_key"])
        fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        existing = self.idempotency.get(key)
        if existing:
            old_fingerprint, provider_ref = existing
            if old_fingerprint != fingerprint:
                raise ValueError("idempotency parameters changed")
            obj = self.objects[provider_ref]
        else:
            if ref in self.orders:
                raise ValueError("merchant order reference already used")
            provider_ref = "mock_" + uuid4().hex
            obj = {
                "merchant_order_ref": ref,
                "provider_payment_ref": provider_ref,
                "merchant_id": merchant,
                "environment": environment,
                "amount_minor": payload["amount_minor"],
                "currency": payload["currency"],
                "order_id": payload["order_id"],
                "organization_id": payload["organization_id"],
                "account_id": payload["account_id"],
                "state": "pending",
                "checkout": {
                    "kind": "redirect",
                    "destination": "https://checkout.mock.nexus.test/pay/" + provider_ref,
                    "expires_at": payload["expires_at"],
                },
                "reversed_minor": 0,
                "disputed_minor": 0,
                "reversal_state": "none",
                "dispute_state": "none",
            }
            self.orders[ref] = provider_ref
            self.objects[provider_ref] = obj
            self.idempotency[key] = (fingerprint, provider_ref)
        return {
            "provider_payment_ref": obj["provider_payment_ref"],
            "checkout": obj["checkout"],
            "state": obj["state"],
        }

    def fact(self, ref):
        provider_ref = self.orders.get(ref, ref)
        return self.objects[provider_ref]

    def paid(self, ref, *, transaction_id=None):
        obj = self.fact(ref)
        obj["state"] = "succeeded"
        obj["transaction_id"] = transaction_id or obj.get("transaction_id") or "txn_" + uuid4().hex
        obj["received_at"] = datetime.now(UTC).isoformat()
        return obj

    def extra_payment(self, ref):
        obj = copy.deepcopy(self.fact(ref))
        obj["provider_payment_ref"] = "mock_extra_" + uuid4().hex
        obj["transaction_id"] = "txn_extra_" + uuid4().hex
        obj["state"] = "succeeded"
        obj["received_at"] = datetime.now(UTC).isoformat()
        self.objects[obj["provider_payment_ref"]] = obj
        return obj

    def notice(self, ref, *, event_id=None):
        obj = copy.deepcopy(self.fact(ref))
        for key in ("checkout", "observed_at", "received_at", "fee_minor", "net_minor"):
            obj.pop(key, None)
        obj["event_id"] = event_id or "evt_" + uuid4().hex
        return obj

    @staticmethod
    def signed_notice(values, secret, *, mode="raw_json", timestamp=None):
        timestamp = int(datetime.now(UTC).timestamp()) if timestamp is None else timestamp
        if mode == "raw_json":
            raw = json.dumps(values, separators=(",", ":")).encode()
            content_type = "application/json"
        else:
            raw = urlencode(values).encode()
            content_type = "application/x-www-form-urlencoded"
        signature = hmac.new(
            secret.encode(), str(timestamp).encode() + b"." + raw, hashlib.sha256
        ).hexdigest()
        return raw, {
            "X-Mock-Signature": f"t={timestamp},v1={signature}",
            "Content-Type": content_type,
        }

    async def _handle(self, reader, writer):
        try:
            header = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
            lines = header.decode("latin-1").split("\r\n")
            method, path, _ = lines[0].split(" ", 2)
            headers = {
                name.strip().lower(): value.strip()
                for name, value in (line.split(":", 1) for line in lines[1:] if ":" in line)
            }
            size = int(headers.get("content-length", "0"))
            if method != "POST" or not 0 <= size <= 65536:
                raise ValueError("unsupported request")
            payload = json.loads(await asyncio.wait_for(reader.readexactly(size), 5))
            key = headers.get("authorization", "").removeprefix("Bearer ")
            identity = self.keys.get(key)
            if identity is None:
                status, data = 401, {"error": "unauthorized"}
            else:
                self.used_keys.append((path, key))
                merchant, environment = identity
                if path == "/payments/create":
                    self.create_calls += 1
                    data = self._create(payload, merchant, environment)
                    self.create_started.set()
                    if self.create_barrier:
                        await self.create_barrier.wait()
                    if self.drop_create_responses:
                        self.drop_create_responses -= 1
                        return
                    status = 200
                elif path == "/payments/query":
                    self.query_calls += 1
                    ref = payload.get("provider_payment_ref") or self.orders.get(
                        payload["merchant_order_ref"]
                    )
                    obj = self.objects.get(ref)
                    if obj and obj["merchant_id"] == merchant and obj["environment"] == environment:
                        data = copy.deepcopy(obj)
                        data["lookup_result"] = "found"
                        data["observed_at"] = datetime.now(UTC).isoformat()
                    else:
                        data = {
                            "lookup_result": "not_found",
                            "merchant_id": merchant,
                            "environment": environment,
                            "merchant_order_ref": payload["merchant_order_ref"],
                        }
                    if self.query_override:
                        data = self.query_override(data)
                    self.query_started.set()
                    if self.query_barrier:
                        await self.query_barrier.wait()
                    status = 200
                elif path == "/control/paid":
                    obj = self.fact(payload["merchant_order_ref"])
                    if obj["merchant_id"] != merchant or obj["environment"] != environment:
                        raise ValueError("wrong merchant")
                    self.paid(payload["merchant_order_ref"])
                    status, data = 200, {"updated": True}
                else:
                    status, data = 404, {"error": "not_found"}
        except (
            ValueError,
            KeyError,
            TimeoutError,
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
        ):
            status, data = 400, {"error": "invalid_request"}
        finally:
            if "status" in locals():
                body = json.dumps(data, separators=(",", ":")).encode()
                writer.write(
                    (
                        f"HTTP/1.1 {status} Mock\r\nContent-Type: application/json\r\n"
                        f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
                    ).encode()
                    + body
                )
                await writer.drain()
            writer.close()
            await writer.wait_closed()


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=59090)
    parser.add_argument("--merchant", default="mock-merchant")
    args = parser.parse_args()
    key = os.environ.get("NEXUS_MOCK_PROVIDER_API_KEY", "")
    if len(key) < 32:
        parser.error(
            "set NEXUS_MOCK_PROVIDER_API_KEY to a test credential of at least 32 characters"
        )
    provider = await MockPaymentProvider({key: (args.merchant, "test")}).start(args.port)
    print("TEST ONLY, volatile state. Mock provider listening at " + provider.url, flush=True)
    try:
        async with provider.server:
            await provider.server.serve_forever()
    finally:
        await provider.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
