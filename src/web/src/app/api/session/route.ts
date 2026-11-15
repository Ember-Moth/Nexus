// Login, CSRF bootstrap and logout for the browser app.
//
// POST   /api/session  {login, password} -> creates a Nexus management session and stores the
//                      access token in an httpOnly cookie. The token never reaches JavaScript.
// GET    /api/session  -> {csrf_token} for an existing session (same-origin readable only).
// DELETE /api/session  -> revokes the Nexus session (DELETE /auth/sessions/current) and clears
//                      the cookies even if the API call fails.
import { NextResponse, type NextRequest } from "next/server";

import { authRequestHeaders } from "@/lib/server/auth-headers";
import { cookieSecure, csrfCookieName, nexusApiUrl } from "@/lib/server/config";
import {
  bffError,
  clearSessionCookies,
  hasValidCsrf,
  isSameOrigin,
  newCsrfToken,
  readSessionToken,
  setSessionCookies,
} from "@/lib/server/session";

const noStore = { "cache-control": "no-store" };

export async function GET(request: NextRequest) {
  const token = readSessionToken(request);
  if (!token) {
    return bffError(401, "unauthorized", "No active session");
  }
  const existing = request.cookies.get(csrfCookieName())?.value;
  const csrf = existing ?? newCsrfToken();
  const response = NextResponse.json({ authenticated: true, csrf_token: csrf }, { headers: noStore });
  if (!existing) {
    // The CSRF cookie was lost: issue a fresh secret as a session cookie.
    response.cookies.set(csrfCookieName(), csrf, {
      httpOnly: true,
      secure: cookieSecure(),
      sameSite: "strict",
      path: "/",
    });
  }
  return response;
}

export async function POST(request: NextRequest) {
  if (!isSameOrigin(request)) {
    return bffError(403, "csrf_failed", "Cross-site login requests are not allowed");
  }
  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return bffError(422, "validation_error", "Request body must be JSON");
  }
  const { login, password, turnstile_token: turnstileToken } = (body ?? {}) as {
    login?: unknown;
    password?: unknown;
    turnstile_token?: unknown;
  };
  if (typeof login !== "string" || typeof password !== "string") {
    return bffError(422, "validation_error", "login and password are required");
  }
  const credentials =
    typeof turnstileToken === "string" && turnstileToken
      ? { login, password, turnstile_token: turnstileToken }
      : { login, password };

  let upstream: Response;
  try {
    upstream = await fetch(`${nexusApiUrl()}/api/v1/auth/sessions`, {
      method: "POST",
      headers: authRequestHeaders(request.headers),
      body: JSON.stringify(credentials),
      cache: "no-store",
    });
  } catch {
    return bffError(502, "bff_upstream_unavailable", "Nexus API is unreachable");
  }

  if (upstream.status !== 201) {
    // Pass the API error envelope (including trace_id) through unchanged.
    const text = await upstream.text();
    return new NextResponse(text, {
      status: upstream.status,
      headers: {
        ...noStore,
        "content-type": upstream.headers.get("content-type") ?? "application/json",
        ...(upstream.headers.get("retry-after") ? { "retry-after": upstream.headers.get("retry-after")! } : {}),
      },
    });
  }

  const created = (await upstream.json()) as { access_token: string; expires_at: string };
  const csrf = newCsrfToken();
  const response = NextResponse.json(
    { expires_at: created.expires_at, csrf_token: csrf },
    { status: 201, headers: noStore },
  );
  setSessionCookies(response, created.access_token, csrf, new Date(created.expires_at));
  return response;
}

export async function DELETE(request: NextRequest) {
  const token = readSessionToken(request);
  if (token && !hasValidCsrf(request)) {
    return bffError(403, "csrf_failed", "Missing or invalid CSRF token");
  }
  if (token) {
    try {
      await fetch(`${nexusApiUrl()}/api/v1/auth/sessions/current`, {
        method: "DELETE",
        headers: { authorization: `Bearer ${token}` },
        cache: "no-store",
      });
    } catch {
      // The cookie is cleared below either way; the server-side session then expires on its own.
    }
  }
  const response = new NextResponse(null, { status: 204, headers: noStore });
  clearSessionCookies(response);
  return response;
}
