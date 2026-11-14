"use client";

import { useQuery } from "@tanstack/react-query";
import { MenuIcon } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useMemo, useState, type ReactNode } from "react";

import { EmptyState, ErrorAlert } from "@/components/feedback";
import { StatusBadge } from "@/components/status-badge";
import { Button } from "@/components/ui/button";
import { Sheet, SheetContent, SheetTitle, SheetTrigger } from "@/components/ui/sheet";
import { Skeleton } from "@/components/ui/skeleton";
import { useT } from "@/i18n/provider";
import { call, nexus } from "@/lib/api/client";
import { isNotFound } from "@/lib/api/errors";
import { useMe } from "@/lib/api/hooks";
import type { Role } from "@/lib/api/types";
import { cn } from "@/lib/utils";

import { findNavItem, orgNav, platformNav, visibleNav, type NavGroup } from "./nav";
import { UserMenu } from "./user-menu";
import {
  membershipRole,
  platformRolesOf,
  rememberOrganization,
  WorkspaceProvider,
  type Workspace,
} from "./workspace";
import { WorkspaceSwitcher } from "./workspace-switcher";

function NavLinks({ groups, base, active, onNavigate }: {
  groups: NavGroup[];
  base: string;
  active?: string;
  onNavigate?: () => void;
}) {
  const t = useT();
  return (
    <nav aria-label={t("nav.main")} className="flex flex-col gap-4 px-3 pb-3">
      {groups.map((group) => (
        <div key={group.label} className="flex flex-col gap-px">
          <div className="flex h-7 items-center px-2 text-xs font-medium text-muted-foreground">{t(group.label)}</div>
          {group.items.map((item) => {
            const current = item.segment === active;
            const Icon = item.icon;
            return (
              <Link
                key={item.segment}
                href={`${base}/${item.segment}`}
                onClick={onNavigate}
                aria-current={current ? "page" : undefined}
                className={cn(
                  "flex h-8 items-center gap-2 rounded-md px-2 text-sm text-foreground/80 outline-none hover:bg-muted hover:text-foreground focus-visible:ring-3 focus-visible:ring-ring/50",
                  current && "bg-muted font-semibold text-foreground",
                )}
              >
                <Icon className="size-4" aria-hidden />
                <span className="flex-1">{t(item.label)}</span>
              </Link>
            );
          })}
        </div>
      ))}
    </nav>
  );
}

function SidebarBody({ groups, base, active, onNavigate }: {
  groups: NavGroup[];
  base: string;
  active?: string;
  onNavigate?: () => void;
}) {
  return (
    <div className="flex h-full flex-col">
      <div className="p-3 pb-2">
        <WorkspaceSwitcher onNavigate={onNavigate} />
      </div>
      <div className="flex-1 overflow-y-auto pt-1">
        <NavLinks groups={groups} base={base} active={active} onNavigate={onNavigate} />
      </div>
      <div className="border-t p-3">
        <UserMenu />
      </div>
    </div>
  );
}

function ShellSkeleton() {
  return (
    <div className="flex min-h-screen">
      <div className="hidden w-64 shrink-0 border-r bg-sidebar p-3 md:block">
        <Skeleton className="h-12 w-full" />
        <div className="mt-6 flex flex-col gap-2">
          {Array.from({ length: 8 }, (_, i) => (
            <Skeleton key={i} className="h-7 w-full" />
          ))}
        </div>
      </div>
      <div className="flex-1 p-8">
        <Skeleton className="h-8 w-48" />
        <Skeleton className="mt-6 h-32 w-full max-w-4xl" />
      </div>
    </div>
  );
}

/**
 * Two-level console frame: the platform admin area and per-organization workspaces. The nav
 * is computed from GET /me (platform roles + memberships).
 */
export function ConsoleShell({ kind, orgId, children }: { kind: "org" | "platform"; orgId?: string; children: ReactNode }) {
  const t = useT();
  const pathname = usePathname();
  const me = useMe();
  const [mobileOpen, setMobileOpen] = useState(false);

  const organization = useQuery({
    queryKey: ["organization", orgId],
    queryFn: async () =>
      (
        await call(() =>
          nexus.GET("/api/v1/organizations/{organization_id}", { params: { path: { organization_id: orgId! } } }),
        )
      ).data,
    enabled: kind === "org" && Boolean(orgId),
  });

  useEffect(() => {
    if (kind === "org" && orgId && organization.data) rememberOrganization(orgId);
  }, [kind, orgId, organization.data]);

  const workspace = useMemo<Workspace | null>(() => {
    if (!me.data) return null;
    const platformRoles = platformRolesOf(me.data);
    const orgRole = kind === "org" && orgId ? membershipRole(me.data, orgId) : undefined;
    const roles: Role[] = [...platformRoles, ...(orgRole ? [orgRole] : [])];
    return {
      kind,
      me: me.data,
      platformRoles,
      orgId,
      organization: organization.data,
      orgRole,
      roles,
      platformViewer: kind === "org" && !orgRole && platformRoles.length > 0,
    };
  }, [kind, me.data, orgId, organization.data]);

  if (me.isPending) return <ShellSkeleton />;
  if (me.error || !workspace) {
    return (
      <div className="mx-auto max-w-lg p-8">
        <ErrorAlert error={me.error} onRetry={() => void me.refetch()} />
      </div>
    );
  }

  const base = kind === "platform" ? "/platform" : `/o/${orgId}`;
  const groups = visibleNav(kind === "platform" ? platformNav : orgNav, workspace.roles);
  const segment = pathname.slice(base.length + 1).split("/")[0] || undefined;
  const current = findNavItem(groups, segment);
  const workspaceName = kind === "platform" ? t("switcher.platformName") : (organization.data?.name ?? "");

  return (
    <WorkspaceProvider value={workspace}>
      <a href="#main" className="sr-only focus:not-sr-only focus:absolute focus:z-50 focus:m-2 focus:rounded focus:bg-background focus:p-2">
        {t("common.skipToContent")}
      </a>
      <div className="flex min-h-screen">
        <aside className="sticky top-0 hidden h-screen w-64 shrink-0 border-r bg-sidebar md:block">
          <SidebarBody groups={groups} base={base} active={segment} />
        </aside>
        <div className="flex min-w-0 flex-1 flex-col">
          <header className="flex h-14 shrink-0 items-center gap-3 border-b px-4 md:px-8">
            <Sheet open={mobileOpen} onOpenChange={setMobileOpen}>
              <SheetTrigger asChild>
                <Button variant="ghost" size="icon-sm" className="md:hidden" aria-label={t("common.openNavigation")}>
                  <MenuIcon />
                </Button>
              </SheetTrigger>
              <SheetContent side="left" className="w-72 bg-sidebar p-0" showCloseButton={false}>
                <SheetTitle className="sr-only">{t("nav.main")}</SheetTitle>
                <SidebarBody groups={groups} base={base} active={segment} onNavigate={() => setMobileOpen(false)} />
              </SheetContent>
            </Sheet>
            <nav aria-label={t("common.breadcrumb")} className="flex min-w-0 flex-1 items-center gap-2 text-sm text-muted-foreground">
              <span className="truncate">{workspaceName}</span>
              {current ? (
                <>
                  <span aria-hidden>/</span>
                  <span className="truncate font-medium text-foreground">{t(current.label)}</span>
                </>
              ) : null}
            </nav>
            {workspace.platformViewer ? <StatusBadge tone="info">{t("switcher.platformViewer")}</StatusBadge> : null}
            {organization.data && organization.data.status !== "active" ? (
              <StatusBadge tone={organization.data.status === "provisioning" ? "info" : "warning"}>
                {t(`status.${organization.data.status}` as "status.active")}
              </StatusBadge>
            ) : null}
          </header>
          <main id="main" className="flex-1">
            {kind === "org" && organization.error ? (
              <div className="p-8">
                {isNotFound(organization.error) ? (
                  <EmptyState title={t("errors.notFoundTitle")} description={t("errors.notFoundBody")} />
                ) : (
                  <ErrorAlert error={organization.error} onRetry={() => void organization.refetch()} />
                )}
              </div>
            ) : (
              children
            )}
          </main>
        </div>
      </div>
    </WorkspaceProvider>
  );
}
