"use client";

import { useQuery } from "@tanstack/react-query";
import type { ReactNode } from "react";

import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { useI18n } from "@/i18n/provider";
import type { Locale } from "@/i18n/types";
import { ApiError } from "@/lib/api/errors";
import type { components } from "@/lib/api/schema";

export type AuthOptions = components["schemas"]["PublicAuthOptions"];

/** GET /auth/options through the BFF: email verification, Turnstile and mail delivery flags. */
export function useAuthOptions() {
  return useQuery({
    queryKey: ["auth-options"],
    queryFn: async (): Promise<AuthOptions> => {
      const response = await fetch("/api/auth/options", { cache: "no-store" });
      const body = await response.json().catch(() => null);
      if (!response.ok) throw ApiError.fromResponse(response, body);
      return body as AuthOptions;
    },
    staleTime: 60_000,
    retry: 1,
  });
}

/** POSTs JSON to a public BFF auth route; returns the parsed body or throws ApiError. */
export async function postPublic<T>(url: string, body: unknown): Promise<{ status: number; data: T | null }> {
  let response: Response;
  try {
    response = await fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
    });
  } catch (cause) {
    throw ApiError.network(cause);
  }
  const data = response.status === 204 ? null : await response.json().catch(() => null);
  if (!response.ok) throw ApiError.fromResponse(response, data);
  return { status: response.status, data: data as T | null };
}

/** Frame shared by login, registration and password recovery pages. */
export function AuthLayout({ children, footer }: { children: ReactNode; footer?: ReactNode }) {
  const { t, locale, setLocale } = useI18n();
  return (
    <div className="flex min-h-screen flex-col bg-muted/40">
      <header className="flex h-16 items-center gap-2.5 px-6 md:px-8">
        <span aria-hidden className="flex size-7 items-center justify-center rounded-md bg-primary text-sm font-bold text-primary-foreground">
          N
        </span>
        <span className="flex-1 text-[15px] font-semibold">{t("app.name")}</span>
        <ToggleGroup
          type="single"
          size="sm"
          variant="outline"
          value={locale}
          aria-label={t("login.language")}
          onValueChange={(value) => value && setLocale(value as Locale)}
        >
          <ToggleGroupItem value="zh-CN">中文</ToggleGroupItem>
          <ToggleGroupItem value="en">English</ToggleGroupItem>
        </ToggleGroup>
      </header>
      <div className="flex flex-1 items-center justify-center px-4 pt-6 pb-24">{children}</div>
      {footer ? <footer className="px-8 pb-6 text-center text-xs text-muted-foreground">{footer}</footer> : null}
    </div>
  );
}

export function AuthCard({ title, subtitle, children }: { title: string; subtitle?: string; children: ReactNode }) {
  return (
    <div className="flex w-full max-w-[400px] flex-col gap-5 rounded-2xl border bg-card p-7 shadow-sm">
      <div className="flex flex-col gap-1.5">
        <h1 className="text-[22px] font-semibold tracking-tight">{title}</h1>
        {subtitle ? <p className="text-sm text-muted-foreground">{subtitle}</p> : null}
      </div>
      {children}
    </div>
  );
}
