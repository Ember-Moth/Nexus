import type { Tone } from "@/components/status-badge";
import type { ApiKey } from "@/lib/api/types";

export type KeyState = "active" | "expiring" | "grace" | "rotated" | "revoked" | "expired";

export interface KeyStatus {
  state: KeyState;
  tone: Tone;
  /** Days until expiry for "expiring", ISO time for "grace". */
  days?: number;
  until?: string;
}

const EXPIRING_DAYS = 7;
const DAY = 86_400_000;

/** KeyView has no status field; it is derived from revoked_at, rotated_at and expires_at. */
export function keyStatus(key: ApiKey, now = Date.now()): KeyStatus {
  const expires = key.expires_at ? Date.parse(key.expires_at) : null;
  if (key.revoked_at && Date.parse(key.revoked_at) <= now) {
    return key.rotated_at ? { state: "rotated", tone: "neutral" } : { state: "revoked", tone: "danger" };
  }
  if (expires !== null && expires <= now) {
    return key.rotated_at ? { state: "rotated", tone: "neutral" } : { state: "expired", tone: "neutral" };
  }
  if (key.rotated_at) {
    return { state: "grace", tone: "info", until: key.expires_at ?? undefined };
  }
  if (expires !== null && expires - now < EXPIRING_DAYS * DAY) {
    return { state: "expiring", tone: "warning", days: Math.max(1, Math.ceil((expires - now) / DAY)) };
  }
  return { state: "active", tone: "success" };
}

export const isUsable = (status: KeyStatus) =>
  status.state === "active" || status.state === "expiring" || status.state === "grace";
export const canRotate = (status: KeyStatus) => status.state === "active" || status.state === "expiring";
export const canRevoke = isUsable;
