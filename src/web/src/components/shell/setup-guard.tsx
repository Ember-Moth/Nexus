"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useEffect, type ReactNode } from "react";

import { ErrorAlert, Spinner } from "@/components/feedback";
import { onSetupRequired } from "@/lib/api/client";
import { useSetupStatus } from "@/lib/api/hooks";

/**
 * Keeps the console behind the setup wizard: until the built-in administrator has completed it,
 * every console route goes to /setup. The API enforces the same lock (409 setup_required), and
 * any such answer, e.g. after `nexus setup reset`, also leads here.
 */
export function SetupGuard({ children }: { children: ReactNode }) {
  const router = useRouter();
  const queryClient = useQueryClient();
  const status = useSetupStatus();
  const required = status.data?.required;

  useEffect(() => {
    if (required) router.replace("/setup");
  }, [required, router]);

  useEffect(
    () =>
      onSetupRequired(() => {
        queryClient.clear();
        router.replace("/setup");
      }),
    [queryClient, router],
  );

  if (status.error) {
    return (
      <div className="mx-auto max-w-lg p-8">
        <ErrorAlert error={status.error} onRetry={() => void status.refetch()} />
      </div>
    );
  }
  if (required !== false) {
    return (
      <div className="flex min-h-screen items-center justify-center text-muted-foreground">
        <Spinner />
      </div>
    );
  }
  return children;
}
