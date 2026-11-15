// Public: POST /auth/password/forgot. Always 202 for a well-formed address.
import type { NextRequest } from "next/server";

import { forwardPublicAuth } from "@/lib/server/public-auth";

export async function POST(request: NextRequest) {
  return forwardPublicAuth(request, "auth/password/forgot");
}
