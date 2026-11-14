// Redeems a one-time setup token: POST /auth/password. Unauthenticated, same-origin only.
import { NextResponse, type NextRequest } from "next/server";

import { nexusApiUrl } from "@/lib/server/config";
import { bffError, isSameOrigin } from "@/lib/server/session";

export async function POST(request: NextRequest) {
  if (!isSameOrigin(request)) {
    return bffError(403, "csrf_failed", "Cross-site requests are not allowed");
  }
  let upstream: Response;
  try {
    upstream = await fetch(`${nexusApiUrl()}/api/v1/auth/password`, {
      method: "POST",
      headers: { "content-type": "application/json", accept: "application/json" },
      body: await request.text(),
      cache: "no-store",
    });
  } catch {
    return bffError(502, "bff_upstream_unavailable", "Nexus API is unreachable");
  }
  const body = upstream.status === 204 ? null : await upstream.text();
  return new NextResponse(body, {
    status: upstream.status,
    headers: {
      "cache-control": "no-store",
      ...(body ? { "content-type": upstream.headers.get("content-type") ?? "application/json" } : {}),
    },
  });
}
