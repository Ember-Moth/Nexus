"use client";

import { createContext, useContext } from "react";

import type { CurrentUser, Organization, OrgRole, PlatformRole, Role } from "@/lib/api/types";

export interface Workspace {
  kind: "org" | "platform";
  me: CurrentUser;
  platformRoles: PlatformRole[];
  orgId?: string;
  organization?: Organization;
  /** The caller's active membership role in this organization, if any. */
  orgRole?: OrgRole;
  /** Roles that decide navigation and actions in this workspace. */
  roles: Role[];
  /** A platform role looking at an organization it is not a member of. */
  platformViewer: boolean;
}

const WorkspaceContext = createContext<Workspace | null>(null);
export const WorkspaceProvider = WorkspaceContext.Provider;

export function useWorkspace(): Workspace {
  const value = useContext(WorkspaceContext);
  if (!value) throw new Error("useWorkspace must be used inside the console shell");
  return value;
}

export function useOrgWorkspace(): Workspace & { orgId: string } {
  const workspace = useWorkspace();
  if (!workspace.orgId) throw new Error("This page needs an organization workspace");
  return workspace as Workspace & { orgId: string };
}

export function platformRolesOf(me: CurrentUser): PlatformRole[] {
  return me.platform_roles.filter(
    (role): role is PlatformRole => role === "platform_admin" || role === "platform_finance",
  );
}

export function membershipRole(me: CurrentUser, orgId: string): OrgRole | undefined {
  const membership = me.memberships.find((m) => m.organization_id === orgId && m.status === "active");
  return membership?.role as OrgRole | undefined;
}

export const hasAnyRole = (workspace: Workspace, ...roles: Role[]) =>
  roles.some((role) => workspace.roles.includes(role));

const LAST_ORG_KEY = "nexus.lastOrganization";

/** Per-browser convenience only; failures (private mode) are ignored. */
export function rememberOrganization(orgId: string) {
  try {
    localStorage.setItem(LAST_ORG_KEY, orgId);
  } catch {}
}

export function lastOrganization(): string | null {
  try {
    return localStorage.getItem(LAST_ORG_KEY);
  } catch {
    return null;
  }
}
