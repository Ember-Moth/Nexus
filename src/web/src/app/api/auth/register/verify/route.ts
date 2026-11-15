// Public: POST /auth/registrations/verify with the emailed token and the registration password.
import type { NextRequest } from "next/server";

import { forwardPublicAuth } from "@/lib/server/public-auth";

export async function POST(request: NextRequest) {
  return forwardPublicAuth(request, "auth/registrations/verify", { startSession: true });
}
