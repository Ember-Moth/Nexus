// Server-only settings for the BFF. Never import this module from client components.

/** Base URL of the Nexus management API, without the /api/v1 prefix. */
export function nexusApiUrl(): string {
  return (process.env.NEXUS_API_URL ?? "http://127.0.0.1:8000").replace(/\/+$/, "");
}

/**
 * Secure cookies are the default. Browsers accept them on http://localhost, so local
 * development works unchanged; set NEXUS_COOKIE_SECURE=false only for plain-HTTP hosts
 * other than localhost.
 */
export function cookieSecure(): boolean {
  return process.env.NEXUS_COOKIE_SECURE !== "false";
}

function cookieName(base: string): string {
  // The __Host- prefix pins the cookie to this origin, path "/" and Secure.
  return cookieSecure() ? `__Host-${base}` : base;
}

export const sessionCookieName = () => cookieName("nexus_session");
export const csrfCookieName = () => cookieName("nexus_csrf");

export const CSRF_HEADER = "x-csrf-token";
