import Big from "big.js";

// Amounts arrive as decimal strings (up to 12 fractional digits). They are never converted to
// JavaScript numbers; arithmetic uses big.js and display formatting works on the string.

const DECIMAL = /^-?\d+(\.\d+)?$/;

export interface MoneyParts {
  /** Sign, grouped integer part and the first two decimals, e.g. "−1,234.56". */
  main: string;
  /** Further significant decimals without trailing zeros, e.g. "4318". Never rounded away. */
  extra: string;
  negative: boolean;
}

export function splitMoney(value: string, options: { signed?: boolean } = {}): MoneyParts {
  if (!DECIMAL.test(value)) {
    return { main: value, extra: "", negative: false };
  }
  const negative = value.startsWith("-");
  const [integer, fraction = ""] = (negative ? value.slice(1) : value).split(".");
  const grouped = integer.replace(/^0+(?=\d)/, "").replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  const padded = fraction.padEnd(2, "0");
  const isZero = /^0*$/.test(integer + fraction);
  const sign = negative && !isZero ? "−" : options.signed && !isZero ? "+" : "";
  return {
    main: `${sign}${grouped}.${padded.slice(0, 2)}`,
    extra: padded.slice(2).replace(/0+$/, ""),
    negative: negative && !isZero,
  };
}

export function formatMoney(value: string, options?: { signed?: boolean }): string {
  const parts = splitMoney(value, options);
  return parts.main + parts.extra;
}

export const addMoney = (a: string, b: string) => new Big(a).plus(b).toFixed();
export const subtractMoney = (a: string, b: string) => new Big(a).minus(b).toFixed();
export const compareMoney = (a: string, b: string) => new Big(a).cmp(b);

/** Percentage of `part` in `whole` for progress bars, clamped to 0..100. */
export function percentOf(part: string, whole: string): number {
  const total = new Big(whole);
  if (total.lte(0)) return new Big(part).gt(0) ? 100 : 0;
  const ratio = new Big(part).div(total).times(100);
  const clamped = ratio.gt(100) ? new Big(100) : ratio.lt(0) ? new Big(0) : ratio;
  return Number(clamped.toFixed(2));
}

export type AmountProblem = "required" | "format" | "precision" | "positive" | "min" | "max";

/**
 * Validates user input against the channel or API precision without changing it:
 * "1.001" with 2 decimal places is rejected, not rounded.
 */
export function checkAmount(
  input: string,
  limits: { decimals: number; min?: string; max?: string },
): AmountProblem | null {
  const value = input.trim();
  if (!value) return "required";
  if (!/^\d+(\.\d+)?$/.test(value)) return "format";
  const fraction = value.split(".")[1] ?? "";
  if (fraction.length > limits.decimals) return "precision";
  const amount = new Big(value);
  if (amount.lte(0)) return "positive";
  if (limits.min && amount.lt(limits.min)) return "min";
  if (limits.max && amount.gt(limits.max)) return "max";
  return null;
}
