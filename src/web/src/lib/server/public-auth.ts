// Forwards unauthenticated identity endpoints (registration, password recovery) to the API.
// Server-only. Headers come from authRequestHeaders() so deployment-level changes there
// (client IP forwarding, BFF credentials) apply to every public auth call.
import { NextResponse, type NextRequest } from "next/server";

import { authRequestHeaders } from "./auth-headers";
import { nexusApiUrl } from "./config";
import { bffError, isSameOrigin, newCsrfToken, setSessionCookies } from "./session";

const NO_STORE = { "cache-control": "no-store" };
const PASS_HEADERS = ["content-type", "retry-after", "x-request-id"];

interface SessionPayload {
  access_token: string;
  expires_at: string;
  [field: string]: unknown;
}

/**
 * POSTs the request body to `/api/v1/<path>`. With `startSession`, a 201 that carries an
 * access token is turned into the httpOnly session cookies; the token is removed from the
 * body handed to the browser and a fresh CSRF token is added instead.
 */
export async function forwardPublicAuth(
  request: NextRequest,
  path: string,
  options: { startSession?: boolean } = {},
): Promise<NextResponse> {
  if (!isSameOrigin(request)) {
    return bffError(403, "csrf_failed", "Cross-site requests are not allowed");
  }
  let upstream: Response;
  try {
    upstream = await fetch(`${nexusApiUrl()}/api/v1/${path}`, {
      method: "POST",
      headers: authRequestHeaders(request.headers),
      body: await request.text(),
      cache: "no-store",
    });
  } catch {
    return bffError(502, "bff_upstream_unavailable", "Nexus API is unreachable");
  }

  if (options.startSession && upstream.status === 201) {
    const payload = (await upstream.json()) as SessionPayload;
    const { access_token: accessToken, ...rest } = payload;
    const csrf = newCsrfToken();
    const response = NextResponse.json({ ...rest, csrf_token: csrf }, { status: 201, headers: NO_STORE });
    setSessionCookies(response, accessToken, csrf, new Date(payload.expires_at));
    return response;
  }

  const headers = new Headers(NO_STORE);
  for (const name of PASS_HEADERS) {
    const value = upstream.headers.get(name);
    if (value) headers.set(name, value);
  }
  const body = upstream.status === 204 ? null : await upstream.arrayBuffer();
  return new NextResponse(body, { status: upstream.status, headers });
}

/** GET /api/v1/auth/options: which verification steps the sign-in forms must show. */
export async function forwardAuthOptions(request: NextRequest): Promise<NextResponse> {
  let upstream: Response;
  try {
    upstream = await fetch(`${nexusApiUrl()}/api/v1/auth/options`, {
      headers: authRequestHeaders(request.headers),
      cache: "no-store",
    });
  } catch {
    return bffError(502, "bff_upstream_unavailable", "Nexus API is unreachable");
  }
  return new NextResponse(await upstream.arrayBuffer(), {
    status: upstream.status,
    headers: { ...NO_STORE, "content-type": upstream.headers.get("content-type") ?? "application/json" },
  });
}
