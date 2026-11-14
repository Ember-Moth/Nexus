// Display helpers. Times are shown in UTC because budgets, usage days and exports are UTC.

const pad = (value: number) => String(value).padStart(2, "0");

export function formatDateTime(iso: string | null | undefined, options: { seconds?: boolean } = {}) {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  const base = `${date.getUTCFullYear()}-${pad(date.getUTCMonth() + 1)}-${pad(date.getUTCDate())} ${pad(
    date.getUTCHours(),
  )}:${pad(date.getUTCMinutes())}`;
  return options.seconds === false ? base : `${base}:${pad(date.getUTCSeconds())}`;
}

export function formatDate(iso: string | null | undefined) {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return `${date.getUTCFullYear()}-${pad(date.getUTCMonth() + 1)}-${pad(date.getUTCDate())}`;
}

export function formatTime(iso: string | null | undefined) {
  if (!iso) return "—";
  const date = new Date(iso);
  return `${pad(date.getUTCHours())}:${pad(date.getUTCMinutes())}:${pad(date.getUTCSeconds())}`;
}

/** "7f21b8e4-…-c90a" → "7f21…c90a" for dense tables; the full value stays in title/copy. */
export function shortId(id: string | null | undefined) {
  if (!id) return "—";
  return id.length > 12 ? `${id.slice(0, 4)}…${id.slice(-4)}` : id;
}

/** Start of the current UTC month as an ISO string. */
export function startOfUtcMonth(now = new Date()) {
  return new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), 1)).toISOString();
}

export function startOfUtcDay(daysAgo = 0, now = new Date()) {
  return new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate() - daysAgo)).toISOString();
}

/** Converts "YYYY-MM-DD" (+ optional "HH:mm") entered as UTC into an ISO timestamp. */
export function utcInputToIso(date: string, time = "00:00"): string | null {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date) || !/^\d{2}:\d{2}$/.test(time)) return null;
  const value = new Date(`${date}T${time}:00Z`);
  return Number.isNaN(value.getTime()) ? null : value.toISOString();
}
