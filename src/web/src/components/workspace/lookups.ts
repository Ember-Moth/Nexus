"use client";

import { useQuery } from "@tanstack/react-query";
import { useMemo } from "react";

import { hasAnyRole, type Workspace } from "@/components/shell/workspace";
import { call, nexus } from "@/lib/api/client";
import { orgKey } from "@/lib/api/queries";
import type { components } from "@/lib/api/schema";
import type { ApiKey, Model, Page } from "@/lib/api/types";

export type MemberListItem = components["schemas"]["MemberListItem"];

/**
 * Reads up to `maxPages` cursor pages and concatenates them. Used for lookups (names, pickers)
 * and for client-side filtering where the API has no matching filter. `truncated` tells the
 * caller that more data exists beyond what was read.
 */
export async function collectPages<T>(
  fetchPage: (cursor: string | undefined) => Promise<Page<T>>,
  maxPages = 10,
): Promise<{ items: T[]; truncated: boolean }> {
  const items: T[] = [];
  let cursor: string | undefined;
  for (let page = 0; page < maxPages; page++) {
    const result = await fetchPage(cursor);
    items.push(...result.items);
    if (!result.next_cursor) return { items, truncated: false };
    cursor = result.next_cursor;
  }
  return { items, truncated: true };
}

/**
 * Model catalog. Without an organization (platform admins only) this is the full managed
 * catalog; with one it is the effective set for that organization / project / key context.
 */
export function useModelCatalog(
  orgId: string | undefined,
  context: { projectId?: string; keyId?: string } = {},
  enabled = true,
) {
  const { projectId, keyId } = context;
  return useQuery({
    queryKey: orgId
      ? orgKey(orgId, "models", "catalog", projectId ?? null, keyId ?? null)
      : ["ws", "models", "catalog", "all"],
    queryFn: async () =>
      (
        await collectPages<Model>(
          async (cursor) =>
            (
              await call(() =>
                nexus.GET("/api/v1/models", {
                  params: {
                    query: { organization_id: orgId, project_id: projectId, key_id: keyId, limit: 100, cursor },
                  },
                }),
              )
            ).data,
          5,
        )
      ).items,
    enabled,
    staleTime: 60_000,
  });
}

/**
 * Model names for reports and policies. Platform admins read the full catalog (models outside
 * the organization's current permissions still resolve); everyone else reads the organization's
 * effective catalog. Unknown IDs fall back to short IDs at the call site.
 */
export function useModelNames(workspace: Workspace & { orgId: string }) {
  const admin = hasAnyRole(workspace, "platform_admin");
  const full = useModelCatalog(undefined, {}, admin);
  const scoped = useModelCatalog(workspace.orgId, {}, !admin);
  const data = admin ? full.data : scoped.data;
  return useMemo(() => new Map((data ?? []).map((model) => [model.id, model])), [data]);
}

/** First 100 keys (same cache entry as the quotas page). Finance roles cannot list keys. */
export function useKeyOptions(orgId: string, enabled = true) {
  return useQuery({
    queryKey: orgKey(orgId, "api-keys", "options"),
    queryFn: async () =>
      (
        await call(() =>
          nexus.GET("/api/v1/organizations/{organization_id}/api-keys", {
            params: { path: { organization_id: orgId }, query: { limit: 100 } },
          }),
        )
      ).data.items,
    enabled,
  });
}

export function useKeyNames(workspace: Workspace & { orgId: string }) {
  const canList = hasAnyRole(workspace, "org_owner", "org_developer", "platform_admin");
  const keys = useKeyOptions(workspace.orgId, canList);
  return useMemo(() => new Map<string, ApiKey>((keys.data ?? []).map((key) => [key.id, key])), [keys.data]);
}

/** All members (up to 1000) for pickers and names. Only owners and platform admins may list them. */
export function useMemberOptions(orgId: string, enabled = true) {
  return useQuery({
    queryKey: orgKey(orgId, "members", "all"),
    queryFn: async () =>
      (
        await collectPages<MemberListItem>(async (cursor) =>
          (
            await call(() =>
              nexus.GET("/api/v1/organizations/{organization_id}/members", {
                params: { path: { organization_id: orgId }, query: { limit: 100, cursor } },
              }),
            )
          ).data,
        )
      ).items,
    enabled,
  });
}

/** "1234567" or 1234567 → "1,234,567" without converting large integers to floats. */
export function formatCount(value: number | string | null | undefined): string {
  if (value === null || value === undefined || value === "") return "—";
  const text = String(value);
  if (!/^-?\d+$/.test(text)) return text;
  return text.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
}

/** "0.850000" → "0.85", "1.000000" → "1". Keeps the string exact otherwise. */
export function trimDecimal(value: string): string {
  if (!/^-?\d+\.\d+$/.test(value)) return value;
  return value.replace(/0+$/, "").replace(/\.$/, "");
}

/** Strict decimal input check for amounts that may be zero (e.g. budget limits). */
export function checkDecimal(value: string, decimals = 12): "required" | "format" | "precision" | null {
  const text = value.trim();
  if (!text) return "required";
  if (!/^\d{1,16}(\.\d+)?$/.test(text)) return "format";
  const fraction = text.split(".")[1] ?? "";
  if (fraction.length > decimals) return "precision";
  return null;
}
