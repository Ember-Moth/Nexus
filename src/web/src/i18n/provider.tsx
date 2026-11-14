"use client";

import { useRouter } from "next/navigation";
import { createContext, useCallback, useContext, useMemo, type ReactNode } from "react";

import en from "./messages/en";
import zhCN, { type Messages } from "./messages/zh-CN";
import { LOCALE_COOKIE, type LeafPaths, type Locale } from "./types";

export type MessageKey = LeafPaths<Messages>;
type Vars = Record<string, string | number>;

const dictionaries: Record<Locale, unknown> = { "zh-CN": zhCN, en };

function lookup(dictionary: unknown, key: string): string | undefined {
  let node: unknown = dictionary;
  for (const part of key.split(".")) {
    if (typeof node !== "object" || node === null) return undefined;
    node = (node as Record<string, unknown>)[part];
  }
  return typeof node === "string" ? node : undefined;
}

function interpolate(template: string, vars?: Vars): string {
  if (!vars) return template;
  return template.replace(/\{(\w+)\}/g, (match, name: string) =>
    name in vars ? String(vars[name]) : match,
  );
}

interface I18nValue {
  locale: Locale;
  t: (key: MessageKey, vars?: Vars) => string;
  /** For keys built at runtime (error codes, enum values); undefined when missing. */
  maybeT: (key: string, vars?: Vars) => string | undefined;
  setLocale: (locale: Locale) => void;
}

const I18nContext = createContext<I18nValue | null>(null);

export function I18nProvider({ locale, children }: { locale: Locale; children: ReactNode }) {
  const router = useRouter();
  const maybeT = useCallback(
    (key: string, vars?: Vars) => {
      const text = lookup(dictionaries[locale], key) ?? lookup(zhCN, key);
      return text === undefined ? undefined : interpolate(text, vars);
    },
    [locale],
  );
  const value = useMemo<I18nValue>(
    () => ({
      locale,
      maybeT,
      t: (key, vars) => maybeT(key, vars) ?? key,
      setLocale: (next) => {
        document.cookie = `${LOCALE_COOKIE}=${next}; path=/; max-age=31536000; samesite=lax`;
        router.refresh();
      },
    }),
    [locale, maybeT, router],
  );
  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>;
}

export function useI18n(): I18nValue {
  const value = useContext(I18nContext);
  if (!value) throw new Error("useI18n must be used inside I18nProvider");
  return value;
}

export const useT = () => useI18n().t;
