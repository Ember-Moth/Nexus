"use client";

import { useEffect, useState, type ReactNode } from "react";

import { StatusBadge, type Tone } from "@/components/status-badge";
import { useT } from "@/i18n/provider";
import { formatDateTime, shortId } from "@/lib/format";
import { cn } from "@/lib/utils";

/** Fields shared by price and discount versions. */
export interface Versioned {
  id: string;
  state: string;
  version: number;
  effective_at: string | null;
  published_at: string | null;
  created_at: string;
}

export type Phase = "draft" | "scheduled" | "live" | "ended";

export interface PhaseInfo {
  phase: Phase;
  /** Start of the next published version: the end boundary of this one. */
  until?: string;
}

export const PHASE_TONE: Record<Phase, Tone> = { draft: "neutral", scheduled: "info", live: "success", ended: "neutral" };
const PHASE_DOT: Record<Phase, string> = {
  draft: "border-muted-foreground bg-background",
  scheduled: "border-blue-600 bg-blue-600",
  live: "border-emerald-600 bg-emerald-600",
  ended: "border-muted-foreground/50 bg-muted-foreground/40",
};
const PHASE_ORDER: Record<Phase, number> = { draft: 0, scheduled: 1, live: 2, ended: 3 };

const time = (iso: string | null | undefined) => (iso ? Date.parse(iso) : Number.NaN);

/**
 * Drafts are editable; published versions are immutable and ordered by effective_at. The latest
 * one that has started is live, later ones are scheduled, earlier ones ended when their
 * successor started.
 */
export function phasesOf(items: Versioned[], now: number): Map<string, PhaseInfo> {
  const result = new Map<string, PhaseInfo>();
  const published = items
    .filter((item) => item.state !== "draft" && item.effective_at)
    .sort((a, b) => time(a.effective_at) - time(b.effective_at));
  let liveIndex = -1;
  published.forEach((item, index) => {
    if (time(item.effective_at) <= now) liveIndex = index;
  });
  published.forEach((item, index) => {
    const until = published[index + 1]?.effective_at ?? undefined;
    const phase: Phase = index === liveIndex ? "live" : index > liveIndex ? "scheduled" : "ended";
    result.set(item.id, { phase, until });
  });
  for (const item of items) {
    if (!result.has(item.id)) result.set(item.id, { phase: "draft" });
  }
  return result;
}

/** Drafts first (newest first), then published versions from the latest start backwards. */
export function sortVersions<T extends Versioned>(items: T[], phases: Map<string, PhaseInfo>): T[] {
  return [...items].sort((a, b) => {
    const pa = phases.get(a.id)?.phase ?? "draft";
    const pb = phases.get(b.id)?.phase ?? "draft";
    if (pa !== pb) return PHASE_ORDER[pa] - PHASE_ORDER[pb];
    if (pa === "draft") return time(b.created_at) - time(a.created_at);
    return time(b.effective_at) - time(a.effective_at);
  });
}

/** Latest published start in the scope; a new version must begin after it. */
export function latestStart(items: Versioned[]): string | null {
  let latest: string | null = null;
  for (const item of items) {
    if (item.state !== "draft" && item.effective_at && (!latest || time(item.effective_at) > time(latest))) {
      latest = item.effective_at;
    }
  }
  return latest;
}

/** Current time for phase calculation, refreshed every 30 s so scheduled versions go live on screen. */
export function useNow(intervalMs = 30_000) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(timer);
  }, [intervalMs]);
  return now;
}

const short = (iso: string) => formatDateTime(iso, { seconds: false });

/** "2026-11-01 00:00 → 2026-12-01 00:00", "2026-12-01 00:00 起" or "未发布 · 创建于 …". */
export function useRangeText() {
  const t = useT();
  return (item: Versioned, info: PhaseInfo) => {
    if (info.phase === "draft") return t("pc.timeline.unpublished", { time: short(item.created_at) });
    if (!item.effective_at) return "";
    if (info.until) return t("pc.timeline.range", { from: short(item.effective_at), to: short(info.until) });
    return t("pc.timeline.from", { from: short(item.effective_at) });
  };
}

export function PhaseBadge({ phase }: { phase: Phase }) {
  const t = useT();
  return <StatusBadge tone={PHASE_TONE[phase]}>{t(`pc.timeline.phases.${phase}`)}</StatusBadge>;
}

/**
 * Compact version timeline. With `onSelect` each entry is a button and the selected one is
 * highlighted (detail shown elsewhere); otherwise `actions` render inside each entry.
 */
export function VersionTimeline<T extends Versioned>({
  items,
  now,
  title,
  summary,
  actions,
  selectedId,
  onSelect,
  className,
}: {
  items: T[];
  now: number;
  title?: (item: T) => ReactNode;
  summary?: (item: T) => ReactNode;
  actions?: (item: T, info: PhaseInfo) => ReactNode;
  selectedId?: string;
  onSelect?: (item: T) => void;
  className?: string;
}) {
  const range = useRangeText();
  const phases = phasesOf(items, now);
  const sorted = sortVersions(items, phases);

  return (
    <ol className={cn("flex flex-col gap-0.5", className)}>
      {sorted.map((item) => {
        const info = phases.get(item.id)!;
        const selected = item.id === selectedId;
        const content = (
          <>
            <span aria-hidden className="flex flex-col items-center pt-1">
              <span className={cn("size-2.5 rounded-full border-2", PHASE_DOT[info.phase])} />
            </span>
            <span className="flex min-w-0 flex-1 flex-col gap-0.5">
              <span className="flex flex-wrap items-center gap-2">
                <PhaseBadge phase={info.phase} />
                {title ? title(item) : null}
                <code className="font-mono text-xs text-muted-foreground" title={item.id}>
                  {shortId(item.id)}
                </code>
              </span>
              <span className="text-xs text-muted-foreground tabular-nums">{range(item, info)}</span>
              {summary ? <span className="text-xs text-foreground/80 tabular-nums">{summary(item)}</span> : null}
            </span>
          </>
        );
        return (
          <li key={item.id} className={cn(info.phase === "ended" && !selected && "opacity-80")}>
            {onSelect ? (
              <button
                type="button"
                aria-current={selected ? "true" : undefined}
                onClick={() => onSelect(item)}
                className={cn(
                  "flex w-full gap-3 rounded-lg px-2.5 py-2.5 text-left outline-none hover:bg-muted/60 focus-visible:ring-3 focus-visible:ring-ring/50",
                  selected && "bg-muted",
                )}
              >
                {content}
              </button>
            ) : (
              <div className="flex flex-wrap items-start gap-3 rounded-lg px-2.5 py-2.5">
                <div className="flex min-w-0 flex-1 gap-3">{content}</div>
                {actions ? <div className="flex flex-wrap gap-2">{actions(item, info)}</div> : null}
              </div>
            )}
          </li>
        );
      })}
    </ol>
  );
}
