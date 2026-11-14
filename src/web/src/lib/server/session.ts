// Session and CSRF helpers for the BFF route handlers. Server-only.
import { randomBytes, randomUUID, timingSafeEqual } from "node:crypto";
import { NextResponse, type NextRequest } from "next/server";

import { CSRF_HEADER, cookieSecure, csrfCookieName, sessionCookieName } from "./config";

export function readSessionToken(request: NextRequest): string | undefined {
  return request.cookies.get(sessionCookieName())?.value || undefined;
}

export function newCsrfToken(): string {
  return randomBytes(32).toString("base64url");
}

export function setSessionCookies(
  response: NextResponse,
  accessToken: string,
  csrfToken: string,
  expiresAt: Date,
): void {
  const secure = cookieSecure();
  // Lax keeps the session when the console is opened from a link; writes are protected by
  // the CSRF token and the Origin check below.
  response.cookies.set(sessionCookieName(), accessToken, {
    httpOnly: true,
    secure,
    sameSite: "lax",
    path: "/",
    expires: expiresAt,
  });
  // The CSRF secret is httpOnly too: the browser app receives it from GET /api/session,
  // which other origins cannot read.
  response.cookies.set(csrfCookieName(), csrfToken, {
    httpOnly: true,
    secure,
    sameSite: "strict",
    path: "/",
    expires: expiresAt,
  });
}

export function clearSessionCookies(response: NextResponse): void {
  const secure = cookieSecure();
  for (const name of [sessionCookieName(), csrfCookieName()]) {
    response.cookies.set(name, "", { httpOnly: true, secure, path: "/", maxAge: 0 });
  }
}

/** Rejects cross-site requests: the browser must report a same-origin request. */
export function isSameOrigin(request: NextRequest): boolean {
  const expected = process.env.NEXUS_WEB_ORIGIN ?? request.nextUrl.origin;
  const origin = request.headers.get("origin");
  if (origin) {
    return origin === expected;
  }
  return request.headers.get("sec-fetch-site") === "same-origin";
}

function equalSecret(a: string, b: string): boolean {
  const left = Buffer.from(a);
  const right = Buffer.from(b);
  return left.length === right.length && timingSafeEqual(left, right);
}

/** Double-submit check for state-changing requests. */
export function hasValidCsrf(request: NextRequest): boolean {
  const cookie = request.cookies.get(csrfCookieName())?.value;
  const header = request.headers.get(CSRF_HEADER);
  return Boolean(cookie && header && equalSecret(cookie, header) && isSameOrigin(request));
}

/** Errors produced by the BFF itself use the same envelope as the Nexus API. */
export function bffError(status: number, code: string, message: string): NextResponse {
  const traceId = randomUUID();
  return NextResponse.json(
    { error: { code, message, trace_id: traceId } },
    { status, headers: { "cache-control": "no-store", "x-request-id": traceId } },
  );
}
