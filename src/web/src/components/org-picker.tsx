"use client";

import { useQuery } from "@tanstack/react-query";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useCallback } from "react";

import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { useT } from "@/i18n/provider";
import { call, nexus } from "@/lib/api/client";

/**
 * Organizations visible to the caller (all of them for platform roles), first 100. `label` is
 * the name, plus a short ID when several organizations share it (personal organizations do).
 */
export function useOrganizationOptions() {
  return useQuery({
    queryKey: ["organizations", "options"],
    queryFn: async () =>
      (await call(() => nexus.GET("/api/v1/organizations", { params: { query: { limit: 100 } } }))).data.items,
    select: (items) => {
      const counts = new Map<string, number>();
      for (const org of items) counts.set(org.name, (counts.get(org.name) ?? 0) + 1);
      return items.map((org) => ({
        ...org,
        label: (counts.get(org.name) ?? 0) > 1 ? `${org.name} · ${org.id.slice(0, 8)}` : org.name,
      }));
    },
  });
}

/**
 * Platform pages that act on one organization keep it in `?org=<id>` so the page can be
 * linked and survives reloads. Wrap the page in <Suspense> (useSearchParams).
 */
export function useOrgParam(): [string | undefined, (orgId: string) => void] {
  const params = useSearchParams();
  const router = useRouter();
  const pathname = usePathname();
  const orgId = params.get("org") ?? undefined;
  const setOrgId = useCallback(
    (next: string) => {
      const search = new URLSearchParams(params.toString());
      search.set("org", next);
      router.replace(`${pathname}?${search.toString()}`, { scroll: false });
    },
    [params, pathname, router],
  );
  return [orgId, setOrgId];
}

export function OrgPicker({
  value,
  onChange,
  className,
}: {
  value: string | undefined;
  onChange: (orgId: string) => void;
  className?: string;
}) {
  const t = useT();
  const organizations = useOrganizationOptions();
  return (
    <Select value={value ?? ""} onValueChange={onChange}>
      <SelectTrigger aria-label={t("routes.organization")} className={className ?? "min-w-56"}>
        <span className="text-muted-foreground">{t("routes.organization")}</span>
        <SelectValue placeholder={t("routes.selectOrganization")} />
      </SelectTrigger>
      <SelectContent>
        {(organizations.data ?? []).map((org) => (
          <SelectItem key={org.id} value={org.id}>
            {org.label}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}
