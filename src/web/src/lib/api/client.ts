"use client";

import createClient, { type Middleware } from "openapi-fetch";

import { ApiError } from "./errors";
import type { paths } from "./schema";

/** Same-origin BFF prefix; requests go to /api/nexus/api/v1/... */
export const API_BASE = "/api/nexus";

let csrfToken: string | null = null;
let csrfRequest: Promise<string> | null = null;

/** Called after login with the token returned by POST /api/session. */
export function setCsrfToken(token: string | null) {
  csrfToken = token;
}

export async function getCsrfToken(force = false): Promise<string> {
  if (csrfToken && !force) return csrfToken;
  csrfRequest ??= fetch("/api/session", { credentials: "same-origin", cache: "no-store" })
    .then(async (response) => {
      const body = (await response.json().catch(() => null)) as { csrf_token?: string } | null;
      if (!response.ok || !body?.csrf_token) {
        throw ApiError.fromResponse(response, body);
      }
      csrfToken = body.csrf_token;
      return csrfToken;
    })
    .finally(() => {
      csrfRequest = null;
    });
  return csrfRequest;
}

type UnauthorizedListener = () => void;
const unauthorizedListeners = new Set<UnauthorizedListener>();

/** The console shell subscribes to send the user back to the login page on 401. */
export function onUnauthorized(listener: UnauthorizedListener): () => void {
  unauthorizedListeners.add(listener);
  return () => {
    unauthorizedListeners.delete(listener);
  };
}

const csrfMiddleware: Middleware = {
  async onRequest({ request }) {
    if (request.method !== "GET" && request.method !== "HEAD") {
      request.headers.set("x-csrf-token", await getCsrfToken());
    }
    return request;
  },
  onResponse({ response }) {
    // The BFF marks 401s that concern a credential in the request body (the session is fine).
    if (response.status === 401 && response.headers.get("x-nexus-session-valid") !== "1") {
      csrfToken = null;
      unauthorizedListeners.forEach((listener) => listener());
    }
    return response;
  },
};

/** Typed client for the management API, generated from openapi/openapi.json. */
export const nexus = createClient<paths>({ baseUrl: API_BASE, credentials: "same-origin" });
nexus.use(csrfMiddleware);

export interface ApiResult<T> {
  data: T;
  /** ETag of the returned representation; pass it back as If-Match on conditional writes. */
  etag: string | null;
  status: number;
}

type FetchResult<T> = { data?: T; error?: unknown; response: Response };

/**
 * Runs a request, converts error envelopes to ApiError and exposes the ETag.
 * A stale CSRF token (403 csrf_failed) is refreshed and the request repeated once.
 */
export async function call<T>(request: () => Promise<FetchResult<T>>): Promise<ApiResult<T>> {
  for (let attempt = 0; ; attempt++) {
    let result: FetchResult<T>;
    try {
      result = await request();
    } catch (cause) {
      if (cause instanceof ApiError) throw cause;
      throw ApiError.network(cause);
    }
    const { data, error, response } = result;
    if (response.ok) {
      return { data: data as T, etag: response.headers.get("etag"), status: response.status };
    }
    const apiError = ApiError.fromResponse(response, error);
    if (apiError.code === "csrf_failed" && attempt === 0) {
      await getCsrfToken(true);
      continue;
    }
    throw apiError;
  }
}
