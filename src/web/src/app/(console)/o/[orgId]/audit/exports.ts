import type { ExportJob } from "@/lib/api/types";

// The API has no "list exports" endpoint (see README: API gaps). Export jobs created in this
// browser session are remembered here so the audit page can poll and offer their downloads.
const key = (orgId: string) => `nexus.exports.${orgId}`;

export function rememberedExports(orgId: string): string[] {
  try {
    const raw = sessionStorage.getItem(key(orgId));
    const ids = raw ? (JSON.parse(raw) as unknown) : [];
    return Array.isArray(ids) ? ids.filter((id): id is string => typeof id === "string") : [];
  } catch {
    return [];
  }
}

export function rememberExport(orgId: string, job: ExportJob) {
  try {
    const ids = [job.id, ...rememberedExports(orgId).filter((id) => id !== job.id)].slice(0, 20);
    sessionStorage.setItem(key(orgId), JSON.stringify(ids));
    window.dispatchEvent(new Event("nexus-exports"));
  } catch {}
}

export const isFinished = (job: ExportJob | undefined) =>
  job !== undefined && job.state !== "queued" && job.state !== "running";
