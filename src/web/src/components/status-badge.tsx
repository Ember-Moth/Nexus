import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

export type Tone = "success" | "info" | "warning" | "danger" | "neutral";

const tones: Record<Tone, { badge: string; dot: string }> = {
  success: { badge: "border-emerald-200 bg-emerald-50 text-emerald-800", dot: "bg-emerald-600" },
  info: { badge: "border-blue-200 bg-blue-50 text-blue-800", dot: "bg-blue-600" },
  warning: { badge: "border-amber-200 bg-amber-50 text-amber-800", dot: "bg-amber-600" },
  danger: { badge: "border-red-200 bg-red-50 text-red-800", dot: "bg-red-600" },
  neutral: { badge: "border-border bg-muted text-foreground/80", dot: "bg-muted-foreground/60" },
};

/** Status colours always come with a text label, never colour alone. */
export function StatusBadge({ tone, children, className }: { tone: Tone; children: ReactNode; className?: string }) {
  return (
    <span
      className={cn(
        "inline-flex h-[22px] items-center gap-1.5 rounded-md border px-2 text-xs font-medium whitespace-nowrap",
        tones[tone].badge,
        className,
      )}
    >
      <span aria-hidden className={cn("size-1.5 rounded-full", tones[tone].dot)} />
      {children}
    </span>
  );
}

const statusTones: Record<string, Tone> = {
  active: "success",
  posted: "success",
  ready: "success",
  succeeded: "success",
  credited: "success",
  provisioning: "info",
  processing: "info",
  queued: "info",
  running: "info",
  pending: "info",
  creating: "info",
  suspended: "warning",
  restricted: "warning",
  partially_reversed: "warning",
  unknown: "warning",
  revoked: "danger",
  failed: "danger",
  reversed: "danger",
  disabled: "neutral",
  archived: "neutral",
  expired: "neutral",
  cancelled: "neutral",
  closed: "neutral",
  uncredited: "neutral",
};

export const toneFor = (status: string): Tone => statusTones[status] ?? "neutral";
