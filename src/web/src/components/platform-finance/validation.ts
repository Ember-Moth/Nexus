import Big from "big.js";
import { z } from "zod";

import { checkAmount } from "@/lib/money";

/** The API accepts up to 12 fractional digits for money; inputs are never rounded. */
export const MONEY_DECIMALS = 12;

export const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
export const CURRENCY_PATTERN = /^[A-Z]{3}$/;

/** Validation messages are codes resolved by Field (pf.common.fieldErrors.*). */
export const nonblank = (max = 2000) => z.string().trim().min(1, "required").max(max, "tooLong");

export const uuidString = () => z.string().trim().regex(UUID_PATTERN, "uuid");

export const currencyString = () => z.string().trim().regex(CURRENCY_PATTERN, "currency");

/** Positive decimal string (> 0) with at most 12 decimals, checked without rounding. */
export const positiveMoney = () =>
  z.string().superRefine((value, ctx) => {
    const problem = checkAmount(value, { decimals: MONEY_DECIMALS });
    if (problem) ctx.addIssue({ code: "custom", message: problem });
  });

/** Non-negative decimal string (>= 0), e.g. an upstream statement amount. */
export const nonnegativeMoney = () =>
  z.string().superRefine((value, ctx) => {
    const trimmed = value.trim();
    if (!trimmed) return ctx.addIssue({ code: "custom", message: "required" });
    if (!/^\d+(\.\d+)?$/.test(trimmed)) return ctx.addIssue({ code: "custom", message: "format" });
    if ((trimmed.split(".")[1] ?? "").length > MONEY_DECIMALS) ctx.addIssue({ code: "custom", message: "precision" });
  });

/** |value| of a signed decimal string, still as a string. */
export const absMoney = (value: string) => new Big(value).abs().toFixed();

/** "2026-11-14T13:49:26.469Z" → "2026-11-14T13:49:26" for <input type="datetime-local"> read as UTC. */
export function isoToUtcInput(iso: string | null | undefined): string {
  if (!iso) return "";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  return date.toISOString().slice(0, 19);
}

/** Interprets a datetime-local value as UTC; null when it is not a valid date and time. */
export function utcInputToIsoStrict(value: string): string | null {
  const trimmed = value.trim();
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?$/.test(trimmed)) return null;
  const date = new Date(`${trimmed.length === 16 ? `${trimmed}:00` : trimmed}Z`);
  return Number.isNaN(date.getTime()) ? null : date.toISOString();
}

/** A UTC timestamp entered in a datetime-local input that must not be in the future. */
export const pastUtcInput = () =>
  z.string().superRefine((value, ctx) => {
    if (!value.trim()) return ctx.addIssue({ code: "custom", message: "required" });
    const iso = utcInputToIsoStrict(value);
    if (!iso) return ctx.addIssue({ code: "custom", message: "format" });
    if (new Date(iso).getTime() > Date.now()) ctx.addIssue({ code: "custom", message: "future" });
  });
