// Same-origin proxy for the platform metrics snapshot.
//
// GET /internal/metrics is not under /api/v1 (and not in the OpenAPI contract), so the generic
// /api/nexus proxy refuses it. This handler forwards exactly that one read with the session's
// bearer token; the API itself requires the platform_admin role.
import { NextResponse, type NextRequest } from "next/server";

import { nexusApiUrl } from "@/lib/server/config";
import { bffError, clearSessionCookies, readSessionToken } from "@/lib/server/session";

const FORWARD_RESPONSE_HEADERS = ["content-type", "x-request-id", "retry-after"];

export async function GET(request: NextRequest): Promise<NextResponse> {
  const token = readSessionToken(request);
  if (!token) {
    return bffError(401, "unauthorized", "No active session");
  }

  let upstream: Response;
  try {
    upstream = await fetch(`${nexusApiUrl()}/internal/metrics`, {
      method: "GET",
      headers: { authorization: `Bearer ${token}`, accept: "application/json" },
      cache: "no-store",
      redirect: "manual",
    });
  } catch {
    return bffError(502, "bff_upstream_unavailable", "Nexus API is unreachable");
  }

  const headers = new Headers({ "cache-control": "no-store" });
  for (const name of FORWARD_RESPONSE_HEADERS) {
    const value = upstream.headers.get(name);
    if (value) headers.set(name, value);
  }
  const body = upstream.status === 204 || upstream.status === 304 ? null : await upstream.arrayBuffer();
  const response = new NextResponse(body, { status: upstream.status, headers });
  if (upstream.status === 401) {
    // Expired or revoked session: drop the cookies like the API proxy does.
    clearSessionCookies(response);
  }
  return response;
}
