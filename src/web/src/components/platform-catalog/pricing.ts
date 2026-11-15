import Big from "big.js";

import type { MessageKey } from "@/i18n/provider";

import type { AnyDimension } from "./labels";
import type { PriceRate } from "./types";

// Prices and factors stay decimal strings; big.js is only used to compare and to display a
// percentage. Rules mirror the billing API: unit prices are non-negative with at most 12
// decimals (|value| < 10^16); factors are 0 < f <= 1 with at most 6 decimals.

export type UnitQuantity = PriceRate["unit_quantity"];

const UNIT_PRICE = /^\d{1,16}(\.\d{1,12})?$/;
const FACTOR = /^\d(\.\d{1,6})?$/;
const CURRENCY = /^[A-Z]{3}$/;

export const isUnitPrice = (value: string) => UNIT_PRICE.test(value.trim());
export const isCurrency = (value: string) => CURRENCY.test(value.trim());

export function isFactor(value: string) {
  const text = value.trim();
  if (!FACTOR.test(text)) return false;
  const factor = new Big(text);
  return factor.gt(0) && factor.lte(1);
}

/** "0.850000" → "85" for the factor hint. */
export function factorPercent(value: string) {
  try {
    return new Big(value.trim()).times(100).toFixed().replace(/(\.\d*?)0+$/, "$1").replace(/\.$/, "");
  } catch {
    return null;
  }
}

/** "2.000000000000" → "2"; "0.003000000000" → "0.003". Display only, never sent back as input. */
export function trimDecimal(value: string) {
  return /^\d+\.\d+$/.test(value) ? value.replace(/0+$/, "").replace(/\.$/, "") : value;
}

interface UnitOption {
  quantity: UnitQuantity;
  label: MessageKey;
}

const TOKEN_UNITS: UnitOption[] = [
  { quantity: 1000000, label: "pc.prices.units.token1000000" },
  { quantity: 1000, label: "pc.prices.units.token1000" },
  { quantity: 1, label: "pc.prices.units.token1" },
];

/** Allowed price units per dimension (the API rejects other combinations); first is the default. */
export function unitOptions(dimension: AnyDimension): UnitOption[] {
  if (dimension.endsWith("_tokens")) return TOKEN_UNITS;
  if (dimension.startsWith("guardrail_")) {
    return [
      { quantity: 1000, label: "pc.prices.units.guardrail1000" },
      { quantity: 1, label: "pc.prices.units.guardrail1" },
    ];
  }
  if (dimension.startsWith("container_")) {
    return [
      { quantity: 1, label: "pc.prices.units.minute1" },
      { quantity: 20, label: "pc.prices.units.minute20" },
    ];
  }
  if (dimension === "code_execution_seconds") {
    return [
      { quantity: 3600, label: "pc.prices.units.second3600" },
      { quantity: 1, label: "pc.prices.units.second1" },
    ];
  }
  if (dimension === "vector_storage_byte_seconds") {
    return [{ quantity: 92771293593600, label: "pc.prices.units.gibDay" }];
  }
  // web_search_requests, file_search_calls
  return [
    { quantity: 1000, label: "pc.prices.units.unit1000" },
    { quantity: 1, label: "pc.prices.units.unit1" },
  ];
}

export function unitLabel(dimension: string, quantity: number): MessageKey | null {
  return unitOptions(dimension as AnyDimension).find((o) => o.quantity === quantity)?.label ?? null;
}
