// Same-origin proxy to the Nexus management API.
//
// The browser calls /api/nexus/api/v1/<path>; this handler attaches the bearer token from the
// httpOnly session cookie and forwards only the headers the management API contract uses
// (If-Match, Idempotency-Key, Content-Type, Accept). Writes require the CSRF header.
import { NextResponse, type NextRequest } from "next/server";

import { nexusApiUrl } from "@/lib/server/config";
import { bffError, clearSessionCookies, hasValidCsrf, readSessionToken } from "@/lib/server/session";

type Context = { params: Promise<{ path: string[] }> };

const FORWARD_REQUEST_HEADERS = ["accept", "content-type", "if-match", "idempotency-key"];
const FORWARD_RESPONSE_HEADERS = [
  "content-type",
  "content-disposition",
  "etag",
  "retry-after",
  "x-request-id",
];
const SAFE_SEGMENT = /^[A-Za-z0-9._~:@-]+$/;

function upstreamPath(segments: string[]): string | null {
  if (segments.length < 3 || segments[0] !== "api" || segments[1] !== "v1") {
    return null;
  }
  // Sessions go through /api/session so that the token stays in the cookie.
  if (segments[2] === "auth") {
    return null;
  }
  for (const segment of segments) {
    if (!SAFE_SEGMENT.test(segment) || segment === "." || segment === "..") {
      return null;
    }
  }
  return segments.map(encodeURIComponent).join("/");
}

async function forward(request: NextRequest, context: Context): Promise<NextResponse> {
  const { path } = await context.params;
  const target = upstreamPath(path);
  if (!target) {
    return bffError(404, "not_found", "Unknown API path");
  }
  const token = readSessionToken(request);
  if (!token) {
    return bffError(401, "unauthorized", "No active session");
  }
  const method = request.method.toUpperCase();
  if (method !== "GET" && method !== "HEAD" && !hasValidCsrf(request)) {
    return bffError(403, "csrf_failed", "Missing or invalid CSRF token");
  }

  const headers = new Headers({ authorization: `Bearer ${token}` });
  for (const name of FORWARD_REQUEST_HEADERS) {
    const value = request.headers.get(name);
    if (value) headers.set(name, value);
  }

  let upstream: Response;
  try {
    upstream = await fetch(`${nexusApiUrl()}/${target}${request.nextUrl.search}`, {
      method,
      headers,
      body: method === "GET" || method === "HEAD" ? undefined : await request.arrayBuffer(),
      cache: "no-store",
      redirect: "manual",
    });
  } catch {
    return bffError(502, "bff_upstream_unavailable", "Nexus API is unreachable");
  }

  const responseHeaders = new Headers({ "cache-control": "no-store" });
  for (const name of FORWARD_RESPONSE_HEADERS) {
    const value = upstream.headers.get(name);
    if (value) responseHeaders.set(name, value);
  }
  const body = upstream.status === 204 || upstream.status === 304 ? null : await upstream.arrayBuffer();
  const response = new NextResponse(body, { status: upstream.status, headers: responseHeaders });
  if (upstream.status === 401) {
    // Expired or revoked session: drop the cookies so the app returns to the login page.
    clearSessionCookies(response);
  }
  return response;
}

export const GET = forward;
export const POST = forward;
export const PUT = forward;
export const PATCH = forward;
export const DELETE = forward;
