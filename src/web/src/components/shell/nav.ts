import {
  ActivityIcon,
  BarChart3Icon,
  BoxIcon,
  Building2Icon,
  CpuIcon,
  CreditCardIcon,
  FileTextIcon,
  FilePlusIcon,
  FlagIcon,
  FolderIcon,
  GaugeIcon,
  GitBranchIcon,
  KeyRoundIcon,
  LandmarkIcon,
  LayoutDashboardIcon,
  ListIcon,
  NetworkIcon,
  PercentIcon,
  PlugIcon,
  ReceiptIcon,
  ServerIcon,
  ShieldIcon,
  SlidersHorizontalIcon,
  SplitIcon,
  TagIcon,
  TargetIcon,
  TriangleAlertIcon,
  UserIcon,
  UsersIcon,
  type LucideIcon,
} from "lucide-react";

import type { MessageKey } from "@/i18n/provider";
import type { Role } from "@/lib/api/types";

export interface NavItem {
  /** Route segment under /o/[orgId]/ or /platform/. */
  segment: string;
  label: MessageKey;
  icon: LucideIcon;
  /** Roles that see the item; the API still authorizes every request. */
  roles: Role[];
  /** Pages that are planned in the design but not implemented yet. */
  skeleton?: boolean;
}

export interface NavGroup {
  label: MessageKey;
  items: NavItem[];
}

const O = "org_owner";
const D = "org_developer";
const F = "org_finance";
const PA = "platform_admin";
const PF = "platform_finance";

// Visibility follows the page × role matrix of the approved design (docs: identity-api.md,
// project-credentials.md, billing-api.md, payments.md, operations.md).
export const orgNav: NavGroup[] = [
  {
    label: "nav.groups.workspace",
    items: [
      { segment: "overview", label: "nav.items.overview", icon: LayoutDashboardIcon, roles: [O, D, F, PA, PF] },
      { segment: "usage", label: "nav.items.usage", icon: BarChart3Icon, roles: [O, D, F, PA, PF], skeleton: true },
      { segment: "requests", label: "nav.items.requests", icon: ListIcon, roles: [O, D, F, PA, PF] },
    ],
  },
  {
    label: "nav.groups.develop",
    items: [
      { segment: "projects", label: "nav.items.projects", icon: FolderIcon, roles: [O, D, F, PA, PF] },
      { segment: "service-accounts", label: "nav.items.serviceAccounts", icon: CpuIcon, roles: [O, D, PA] },
      { segment: "keys", label: "nav.items.keys", icon: KeyRoundIcon, roles: [O, D, PA] },
      { segment: "models", label: "nav.items.models", icon: BoxIcon, roles: [O, D, F, PA], skeleton: true },
    ],
  },
  {
    label: "nav.groups.billing",
    items: [
      { segment: "wallet", label: "nav.items.wallet", icon: CreditCardIcon, roles: [O, F, PA, PF] },
      { segment: "recharge", label: "nav.items.recharge", icon: ReceiptIcon, roles: [O, F] },
      { segment: "charges", label: "nav.items.charges", icon: FileTextIcon, roles: [O, D, F, PA, PF], skeleton: true },
      { segment: "budgets", label: "nav.items.budgets", icon: TargetIcon, roles: [O, F, PA, PF], skeleton: true },
    ],
  },
  {
    label: "nav.groups.manage",
    items: [
      { segment: "members", label: "nav.items.members", icon: UsersIcon, roles: [O, PA], skeleton: true },
      { segment: "policies", label: "nav.items.policies", icon: ShieldIcon, roles: [O, D, PA], skeleton: true },
      { segment: "quotas", label: "nav.items.quotas", icon: GaugeIcon, roles: [O, PA], skeleton: true },
      { segment: "audit", label: "nav.items.audit", icon: FileTextIcon, roles: [O, F, PA, PF] },
    ],
  },
];

export const platformNav: NavGroup[] = [
  {
    label: "nav.groups.tenants",
    items: [
      { segment: "organizations", label: "nav.items.organizations", icon: Building2Icon, roles: [PA, PF] },
      { segment: "users", label: "nav.items.users", icon: UserIcon, roles: [PA], skeleton: true },
    ],
  },
  {
    label: "nav.groups.catalog",
    items: [
      { segment: "models", label: "nav.items.catalogModels", icon: BoxIcon, roles: [PA], skeleton: true },
      { segment: "upstreams", label: "nav.items.upstreams", icon: ServerIcon, roles: [PA], skeleton: true },
      { segment: "deployments", label: "nav.items.deployments", icon: GitBranchIcon, roles: [PA], skeleton: true },
      { segment: "routes", label: "nav.items.routes", icon: SplitIcon, roles: [PA], skeleton: true },
    ],
  },
  {
    label: "nav.groups.pricing",
    items: [
      { segment: "sale-prices", label: "nav.items.salePrices", icon: TagIcon, roles: [PA], skeleton: true },
      { segment: "cost-prices", label: "nav.items.costPrices", icon: TagIcon, roles: [PA], skeleton: true },
      { segment: "discounts", label: "nav.items.discounts", icon: PercentIcon, roles: [PA], skeleton: true },
    ],
  },
  {
    label: "nav.groups.gateway",
    items: [{ segment: "clusters", label: "nav.items.clusters", icon: NetworkIcon, roles: [PA], skeleton: true }],
  },
  {
    label: "nav.groups.finance",
    items: [
      { segment: "receiving", label: "nav.items.receiving", icon: LandmarkIcon, roles: [PF], skeleton: true },
      { segment: "funding", label: "nav.items.funding", icon: FilePlusIcon, roles: [PF], skeleton: true },
      { segment: "adjustments", label: "nav.items.adjustments", icon: SlidersHorizontalIcon, roles: [PF], skeleton: true },
      { segment: "reconciliation", label: "nav.items.reconciliation", icon: TriangleAlertIcon, roles: [PF], skeleton: true },
      { segment: "payment-cases", label: "nav.items.paymentCases", icon: FlagIcon, roles: [PF], skeleton: true },
      { segment: "channels", label: "nav.items.channels", icon: PlugIcon, roles: [PA], skeleton: true },
    ],
  },
  {
    label: "nav.groups.ops",
    items: [
      { segment: "audit", label: "nav.items.platformAudit", icon: FileTextIcon, roles: [PA, PF], skeleton: true },
      { segment: "metrics", label: "nav.items.metrics", icon: ActivityIcon, roles: [PA], skeleton: true },
    ],
  },
];

export function visibleNav(groups: NavGroup[], roles: Role[]): NavGroup[] {
  return groups
    .map((group) => ({ ...group, items: group.items.filter((item) => item.roles.some((r) => roles.includes(r))) }))
    .filter((group) => group.items.length > 0);
}

export function findNavItem(groups: NavGroup[], segment: string | undefined): NavItem | undefined {
  return groups.flatMap((group) => group.items).find((item) => item.segment === segment);
}
