// Redirects visitors without a session cookie to the login page. This is only a navigation
// convenience: the BFF route handlers and the Nexus API authorize every request themselves.
import { NextResponse, type NextRequest } from "next/server";

import { sessionCookieName } from "@/lib/server/config";

const PUBLIC_PATHS = ["/login", "/setup-password", "/register", "/forgot-password", "/reset-password"];

export function proxy(request: NextRequest) {
  const { pathname, search } = request.nextUrl;
  const hasSession = Boolean(request.cookies.get(sessionCookieName())?.value);
  const isPublic = PUBLIC_PATHS.some((path) => pathname === path || pathname.startsWith(`${path}/`));

  if (!hasSession && !isPublic) {
    const login = new URL("/login", request.url);
    if (pathname !== "/") {
      login.searchParams.set("next", `${pathname}${search}`);
    }
    return NextResponse.redirect(login);
  }
  return NextResponse.next();
}

export const config = {
  // Skip the BFF endpoints (they answer 401 themselves), Next.js assets and files with an extension.
  matcher: ["/((?!api/|_next/|favicon\\.ico|.*\\.[a-zA-Z0-9]+$).*)"],
};
