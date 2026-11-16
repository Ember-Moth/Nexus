"use client";

import {
  useInfiniteQuery,
  useMutation,
  useQuery,
  type Query,
  type QueryKey,
  type UseMutationOptions,
} from "@tanstack/react-query";
import { useCallback, useMemo, useRef } from "react";

import { call, nexus } from "./client";
import { isRetryable } from "./errors";
import type { Page } from "./types";

/** GET /me: user, platform roles and active memberships. */
export function useMe() {
  return useQuery({
    queryKey: ["me"],
    queryFn: async () => (await call(() => nexus.GET("/api/v1/me"))).data,
    staleTime: 60_000,
  });
}

/** GET /setup: whether the built-in administrator still has to complete the setup wizard. */
export function useSetupStatus() {
  return useQuery({
    queryKey: ["setup"],
    queryFn: async () => (await call(() => nexus.GET("/api/v1/setup"))).data,
    staleTime: 60_000,
  });
}

/**
 * Cursor pagination: each page passes the previous `next_cursor` as `cursor`; filters must stay
 * identical between pages, so they belong in the query key.
 */
export function useCursorList<T>(options: {
  queryKey: QueryKey;
  fetchPage: (cursor: string | undefined) => Promise<Page<T>>;
  enabled?: boolean;
  refetchInterval?: number | false;
}) {
  const query = useInfiniteQuery({
    queryKey: options.queryKey,
    queryFn: ({ pageParam }) => options.fetchPage(pageParam),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    enabled: options.enabled,
    refetchInterval: options.refetchInterval,
  });
  const items = useMemo(() => query.data?.pages.flatMap((page) => page.items) ?? [], [query.data]);
  return { ...query, items };
}

function stableStringify(value: unknown): string {
  if (value === null || typeof value !== "object") return JSON.stringify(value) ?? "undefined";
  if (Array.isArray(value)) return `[${value.map(stableStringify).join(",")}]`;
  const entries = Object.entries(value as Record<string, unknown>)
    .filter(([, v]) => v !== undefined)
    .sort(([a], [b]) => a.localeCompare(b));
  return `{${entries.map(([k, v]) => `${JSON.stringify(k)}:${stableStringify(v)}`).join(",")}}`;
}

function newIdempotencyKey(prefix: string): string {
  // Allowed: 1-128 letters, digits or _.:-
  return `${prefix}:${crypto.randomUUID()}`.slice(0, 128);
}

/**
 * A mutation for commands that require Idempotency-Key.
 *
 * One key is generated per business operation and reused for every retry of the same
 * request (same variables, including the If-Match version). Changing the variables starts a
 * new operation with a new key; a success also retires the key. Requests whose outcome is
 * unknown (network failure, 502-504) are retried automatically with the same key.
 */
export function useIdempotentMutation<TData, TVariables>(
  options: {
    /** Readable prefix of the generated Idempotency-Key, e.g. "issue-key". */
    keyPrefix: string;
    mutationFn: (variables: TVariables, idempotencyKey: string) => Promise<TData>;
  } & Omit<UseMutationOptions<TData, Error, TVariables>, "mutationFn" | "retry">,
) {
  const current = useRef<{ fingerprint: string; key: string } | null>(null);
  const { keyPrefix, mutationFn, onSuccess, ...rest } = options;

  const keyFor = useCallback(
    (variables: TVariables) => {
      const fingerprint = stableStringify(variables);
      if (!current.current || current.current.fingerprint !== fingerprint) {
        current.current = { fingerprint, key: newIdempotencyKey(keyPrefix) };
      }
      return current.current.key;
    },
    [keyPrefix],
  );

  const mutation = useMutation<TData, Error, TVariables>({
    ...rest,
    mutationFn: (variables) => mutationFn(variables, keyFor(variables)),
    retry: (failureCount, error) => failureCount < 2 && isRetryable(error),
    retryDelay: (attempt) => Math.min(1000 * 2 ** attempt, 5000),
    onSuccess: (...args) => {
      current.current = null;
      return onSuccess?.(...args);
    },
  });

  const resetOperation = useCallback(() => {
    current.current = null;
    mutation.reset();
  }, [mutation]);

  return { ...mutation, resetOperation };
}

/**
 * refetchInterval for 202 workflows: poll every 2 s, then 5 s, then 10 s, and stop once
 * `isDone` reports a terminal state.
 */
export function pollUntil<T>(isDone: (data: T | undefined) => boolean) {
  return (query: Query<T, Error, T, QueryKey>) => {
    if (isDone(query.state.data)) return false;
    const count = query.state.dataUpdateCount;
    if (count < 10) return 2_000;
    if (count < 25) return 5_000;
    return 10_000;
  };
}
