"""Bounded outbound transports for payment providers and the loopback mock."""

import asyncio
import json

import httpx

from nexus.shared.errors import ServiceError


class PaymentHttp:
    def __init__(self, base_url, *, timeout=10):
        self._timeout = timeout
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout,
            follow_redirects=False,
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
            trust_env=False,
        )

    async def request(self, method, path, *, headers, payload=None):
        if path not in {"/payments/create", "/payments/query"}:
            raise ValueError("unsupported payment protocol path")
        try:
            async with asyncio.timeout(self._timeout):
                return await self._request(method, path, headers=headers, payload=payload)
        except httpx.HTTPError, ValueError, TimeoutError:
            raise ServiceError(
                "payment_provider_unavailable", "Payment provider result is unavailable", 503
            ) from None

    async def _request(self, method, path, *, headers, payload):
        async with self._client.stream(method, path, headers=headers, json=payload) as response:
            if response.status_code != 200:
                raise ValueError("unexpected provider status")
            chunks = []
            size = 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > 1048576:
                    raise ValueError("provider response exceeds limit")
                chunks.append(chunk)
            value = json.loads(b"".join(chunks))
            if not isinstance(value, dict):
                raise ValueError("provider response is not an object")
            return value

    async def close(self):
        await self._client.aclose()


class ProviderHttp:
    """Bounded TLS requests. Never log credentials or signed checkout URLs."""

    def __init__(self, *, transport=None):
        self.transport = transport

    async def send(self, method, url, *, headers=None, params=None, form=None, content=None):
        # The V1 EPay query carries a merchant secret in the URL. Use the transport
        # directly so the HTTP client's INFO request logger never receives this URL.
        request = httpx.Request(
            method, url, headers=headers, params=params, data=form, content=content
        )
        try:
            async with asyncio.timeout(10):
                if self.transport is not None:
                    return await self._send(self.transport, request)
                async with httpx.AsyncHTTPTransport(trust_env=False) as transport:
                    return await self._send(transport, request)
        except httpx.HTTPError, OSError, TimeoutError, ValueError:
            raise ServiceError(
                "payment_provider_unavailable", "Payment provider result is unavailable", 503
            ) from None

    async def _send(self, transport, request):
        response = await transport.handle_async_request(request)
        try:
            chunks = bytearray()
            async for part in response.aiter_bytes():
                if len(chunks) + len(part) > 1048576:
                    raise ValueError("oversize response")
                chunks.extend(part)
            return response.status_code, response.headers.multi_items(), bytes(chunks)
        finally:
            await response.aclose()
