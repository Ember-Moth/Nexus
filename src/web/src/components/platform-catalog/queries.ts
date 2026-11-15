"use client";

import { useQuery, type QueryClient } from "@tanstack/react-query";

import { call, nexus } from "@/lib/api/client";
import { useCursorList } from "@/lib/api/hooks";

import type { CatalogDeployment, CatalogModel, Credential, Upstream } from "./types";

/** Every catalog query key starts with "catalog" so one write refreshes names everywhere. */
export const catalogKey = (...rest: unknown[]) => ["catalog", ...rest] as const;

export const invalidateCatalog = (queryClient: QueryClient) =>
  queryClient.invalidateQueries({ queryKey: ["catalog"] });

/** Full model catalog by cursor, `limit` per page. */
export function useModels(limit = 100, scope = "options") {
  return useCursorList<CatalogModel>({
    queryKey: catalogKey("models", scope, limit),
    fetchPage: async (cursor) =>
      (await call(() => nexus.GET("/api/v1/models", { params: { query: { limit, cursor } } }))).data,
  });
}

export function useUpstreams(limit = 100, scope = "options") {
  return useCursorList<Upstream>({
    queryKey: catalogKey("upstreams", scope, limit),
    fetchPage: async (cursor) =>
      (await call(() => nexus.GET("/api/v1/upstreams", { params: { query: { limit, cursor } } }))).data,
  });
}

export function useDeployments(
  filters: { modelId?: string; upstreamId?: string },
  options: { limit?: number; scope?: string; enabled?: boolean } = {},
) {
  const { limit = 100, scope = "options", enabled = true } = options;
  return useCursorList<CatalogDeployment>({
    queryKey: catalogKey("deployments", scope, limit, filters.modelId ?? null, filters.upstreamId ?? null),
    enabled,
    fetchPage: async (cursor) =>
      (
        await call(() =>
          nexus.GET("/api/v1/deployments", {
            params: {
              query: { limit, cursor, model_id: filters.modelId, upstream_id: filters.upstreamId },
            },
          }),
        )
      ).data,
  });
}

export function useCredentials(upstreamId: string | undefined, limit = 100) {
  return useCursorList<Credential>({
    queryKey: catalogKey("credentials", upstreamId ?? null, limit),
    enabled: Boolean(upstreamId),
    fetchPage: async (cursor) =>
      (
        await call(() =>
          nexus.GET("/api/v1/upstreams/{upstream_id}/credentials", {
            params: { path: { upstream_id: upstreamId! }, query: { limit, cursor } },
          }),
        )
      ).data,
  });
}

/** Single model with its ETag (If-Match for PATCH). */
export function useModelDetail(modelId: string | undefined) {
  return useQuery({
    queryKey: catalogKey("model", modelId ?? null),
    queryFn: async () =>
      call(() => nexus.GET("/api/v1/models/{model_id}", { params: { path: { model_id: modelId! } } })),
    enabled: Boolean(modelId),
    staleTime: 0,
  });
}

export function useUpstreamDetail(upstreamId: string | undefined) {
  return useQuery({
    queryKey: catalogKey("upstream", upstreamId ?? null),
    queryFn: async () =>
      call(() =>
        nexus.GET("/api/v1/upstreams/{upstream_id}", { params: { path: { upstream_id: upstreamId! } } }),
      ),
    enabled: Boolean(upstreamId),
    staleTime: 0,
  });
}

export function useDeploymentDetail(deploymentId: string | undefined) {
  return useQuery({
    queryKey: catalogKey("deployment", deploymentId ?? null),
    queryFn: async () =>
      call(() =>
        nexus.GET("/api/v1/deployments/{deployment_id}", {
          params: { path: { deployment_id: deploymentId! } },
        }),
      ),
    enabled: Boolean(deploymentId),
    staleTime: 0,
  });
}

/** Conditional writes use the representation's ETag, falling back to the quoted version. */
export const etagOf = (result: { etag: string | null } | undefined, version: number | undefined) =>
  result?.etag ?? (version !== undefined ? `"${version}"` : null);
