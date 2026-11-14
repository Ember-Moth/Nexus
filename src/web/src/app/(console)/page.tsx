"use client";

import { useQuery } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useEffect, useMemo } from "react";

import { EmptyState, ErrorAlert, Spinner } from "@/components/feedback";
import { lastOrganization, platformRolesOf } from "@/components/shell/workspace";
import { useSignOut } from "@/components/shell/user-menu";
import { Button } from "@/components/ui/button";
import { useT } from "@/i18n/provider";
import { call, nexus } from "@/lib/api/client";
import { useMe } from "@/lib/api/hooks";

/** Entry point: last used organization, else the first membership, else the platform area. */
export default function HomePage() {
  const t = useT();
  const router = useRouter();
  const me = useMe();
  const { signOut } = useSignOut();
  const memberships = useMemo(() => me.data?.memberships.filter((m) => m.status === "active") ?? [], [me.data]);
  const isPlatform = me.data ? platformRolesOf(me.data).length > 0 : false;

  // Platform roles without memberships still have organizations to look at.
  const organizations = useQuery({
    queryKey: ["organizations", "home"],
    queryFn: async () =>
      (await call(() => nexus.GET("/api/v1/organizations", { params: { query: { limit: 1 } } }))).data,
    enabled: Boolean(me.data) && memberships.length === 0 && !isPlatform,
  });

  useEffect(() => {
    if (!me.data) return;
    const last = lastOrganization();
    const target =
      memberships.find((m) => m.organization_id === last)?.organization_id ??
      memberships[0]?.organization_id ??
      (isPlatform ? null : organizations.data?.items[0]?.id);
    if (target) router.replace(`/o/${target}/overview`);
    else if (isPlatform) router.replace("/platform/organizations");
  }, [me.data, memberships, isPlatform, organizations.data, router]);

  if (me.error) {
    return (
      <div className="mx-auto max-w-lg p-8">
        <ErrorAlert error={me.error} onRetry={() => void me.refetch()} />
      </div>
    );
  }
  const nothing = me.data && memberships.length === 0 && !isPlatform && organizations.data?.items.length === 0;
  if (nothing) {
    return (
      <EmptyState
        className="min-h-screen justify-center"
        title={t("home.noWorkspaceTitle")}
        description={t("home.noWorkspaceBody")}
        action={<Button variant="outline" onClick={() => void signOut()}>{t("user.signOut")}</Button>}
      />
    );
  }
  return (
    <div className="flex min-h-screen items-center justify-center gap-2 text-sm text-muted-foreground">
      <Spinner />
      {t("home.redirecting")}
    </div>
  );
}
