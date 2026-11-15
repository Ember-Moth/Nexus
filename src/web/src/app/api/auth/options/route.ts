// Public: verification requirements for the sign-in, registration and recovery forms.
import type { NextRequest } from "next/server";

import { forwardAuthOptions } from "@/lib/server/public-auth";

export async function GET(request: NextRequest) {
  return forwardAuthOptions(request);
}
