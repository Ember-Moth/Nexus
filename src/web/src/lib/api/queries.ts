"use client";

import { useQuery } from "@tanstack/react-query";

import { call, nexus } from "./client";

/** Query keys are scoped by organization so switching workspaces never mixes data. */
export const orgKey = (orgId: string, ...rest: unknown[]) => ["org", orgId, ...rest] as const;

/** First 100 projects, for filters and pickers. */
export function useProjectOptions(orgId: string, enabled = true) {
  return useQuery({
    queryKey: orgKey(orgId, "projects", "options"),
    queryFn: async () =>
      (
        await call(() =>
          nexus.GET("/api/v1/organizations/{organization_id}/projects", {
            params: { path: { organization_id: orgId }, query: { limit: 100 } },
          }),
        )
      ).data.items,
    enabled,
  });
}

/** First 100 service accounts (optionally in one project), for filters and pickers. */
export function useServiceAccountOptions(orgId: string, projectId?: string, enabled = true) {
  return useQuery({
    queryKey: orgKey(orgId, "service-accounts", "options", projectId ?? null),
    queryFn: async () =>
      (
        await call(() =>
          nexus.GET("/api/v1/organizations/{organization_id}/service-accounts", {
            params: { path: { organization_id: orgId }, query: { limit: 100, project_id: projectId } },
          }),
        )
      ).data.items,
    enabled,
  });
}

export function useAccount(orgId: string, enabled = true) {
  return useQuery({
    queryKey: orgKey(orgId, "account"),
    queryFn: async () =>
      (
        await call(() =>
          nexus.GET("/api/v1/organizations/{organization_id}/account", {
            params: { path: { organization_id: orgId } },
          }),
        )
      ).data,
    enabled,
  });
}
