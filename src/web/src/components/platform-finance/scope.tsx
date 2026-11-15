"use client";

import { useQuery } from "@tanstack/react-query";
import { Building2Icon } from "lucide-react";
import { useCallback, useMemo } from "react";

import { EmptyState } from "@/components/feedback";
import { useOrganizationOptions } from "@/components/org-picker";
import { useWorkspace } from "@/components/shell/workspace";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { useT } from "@/i18n/provider";
import { call, nexus } from "@/lib/api/client";
import { shortId } from "@/lib/format";

const ALL = "__all";

/** Organization filter with an explicit "all organizations" choice (platform-wide lists). */
export function OrgFilter({
  value,
  onChange,
  className,
}: {
  value: string | undefined;
  onChange: (orgId: string | undefined) => void;
  className?: string;
}) {
  const t = useT();
  const organizations = useOrganizationOptions();
  return (
    <Select value={value ?? ALL} onValueChange={(next) => onChange(next === ALL ? undefined : next)}>
      <SelectTrigger aria-label={t("pf.common.organization")} className={className ?? "min-w-56"}>
        <span className="text-muted-foreground">{t("pf.common.organization")}</span>
        <SelectValue />
      </SelectTrigger>
      <SelectContent>
        <SelectItem value={ALL}>{t("pf.common.allOrganizations")}</SelectItem>
        {(organizations.data ?? []).map((org) => (
          <SelectItem key={org.id} value={org.id}>
            {org.label}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}

/** id → organization name from the cached option list; falls back to a short id. */
export function useOrgName() {
  const organizations = useOrganizationOptions();
  const names = useMemo(
    () => new Map((organizations.data ?? []).map((org) => [org.id, org.label] as const)),
    [organizations.data],
  );
  return useCallback((id: string | null | undefined) => (id ? (names.get(id) ?? shortId(id)) : "—"), [names]);
}

/** Shown instead of the page body until an organization is chosen in the picker. */
export function PickOrganization() {
  const t = useT();
  return (
    <div className="rounded-xl border bg-card">
      <EmptyState
        icon={<Building2Icon className="size-5" />}
        title={t("pf.common.pickOrgTitle")}
        description={t("pf.common.pickOrgBody")}
      />
    </div>
  );
}

/**
 * Login names for user ids. GET /users is platform-admin only; for other roles (platform
 * finance) only the caller's own id resolves and the rest are shown as short ids.
 */
export function useUserLabels() {
  const workspace = useWorkspace();
  const isAdmin = workspace.platformRoles.includes("platform_admin");
  const users = useQuery({
    queryKey: ["users", "labels"],
    queryFn: async () => (await call(() => nexus.GET("/api/v1/users", { params: { query: { limit: 100 } } }))).data.items,
    enabled: isAdmin,
    staleTime: 5 * 60_000,
    retry: false,
  });
  const me = workspace.me.user;
  return useMemo(() => {
    const logins = new Map((users.data ?? []).map((user) => [user.id, user.login] as const));
    logins.set(me.id, me.login);
    return (id: string | null | undefined) => (id ? logins.get(id) : undefined);
  }, [users.data, me.id, me.login]);
}

/** A person (by user id) or, for gateway postings, the named system actor. */
export function Actor({
  id,
  systemActor,
  label,
}: {
  id: string | null | undefined;
  systemActor?: string | null;
  label: (id: string | null | undefined) => string | undefined;
}) {
  const t = useT();
  if (id) {
    const login = label(id);
    return login ? (
      <span title={id}>{login}</span>
    ) : (
      <code className="font-mono text-xs" title={id}>
        {shortId(id)}
      </code>
    );
  }
  if (systemActor) {
    return (
      <span className="inline-flex items-center gap-1.5">
        <span className="rounded border bg-muted px-1 text-[11px] text-muted-foreground">{t("pf.common.system")}</span>
        <code className="font-mono text-xs">{systemActor}</code>
      </span>
    );
  }
  return <span className="text-muted-foreground">—</span>;
}
