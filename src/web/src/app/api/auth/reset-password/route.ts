// Public: POST /auth/password/reset with the emailed token and a new password (204).
import type { NextRequest } from "next/server";

import { forwardPublicAuth } from "@/lib/server/public-auth";

export async function POST(request: NextRequest) {
  return forwardPublicAuth(request, "auth/password/reset");
}
