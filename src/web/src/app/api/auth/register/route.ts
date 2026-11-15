// Public: POST /auth/registrations. 201 starts a session (cookies); 202 means a verification
// email was sent and /auth/registrations/verify must follow.
import type { NextRequest } from "next/server";

import { forwardPublicAuth } from "@/lib/server/public-auth";

export async function POST(request: NextRequest) {
  return forwardPublicAuth(request, "auth/registrations", { startSession: true });
}
