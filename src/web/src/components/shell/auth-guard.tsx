"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useEffect, type ReactNode } from "react";

import { onUnauthorized } from "@/lib/api/client";

/** Any 401 from the BFF (expired or revoked session) returns the user to the login page. */
export function AuthGuard({ children }: { children: ReactNode }) {
  const router = useRouter();
  const queryClient = useQueryClient();
  useEffect(
    () =>
      onUnauthorized(() => {
        queryClient.clear();
        const next = `${window.location.pathname}${window.location.search}`;
        router.replace(`/login?expired=1&next=${encodeURIComponent(next)}`);
      }),
    [queryClient, router],
  );
  return children;
}
