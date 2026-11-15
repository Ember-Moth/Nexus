import type { components } from "./schema";

type Schemas = components["schemas"];

// Every type here is generated from the OpenAPI contract (openapi/openapi.json).

export type CurrentUser = Schemas["CurrentUser"];
export type Membership = Schemas["MemberView"];
export type Organization = Schemas["OrganizationView"];
export type Project = Schemas["ProjectView"];
export type ServiceAccount = Schemas["ServiceAccountView"];
export type ApiKey = Schemas["KeyView"];
export type ApiKeyIssued = Schemas["KeyIssued"];
export type Account = Schemas["AccountView"];
export type Budget = Schemas["BudgetView"];
export type BudgetPeriod = Schemas["BudgetPeriodView"];
export type PaymentMethod = Schemas["PaymentMethod"];
export type PaymentOrder = Schemas["PaymentOrderView"];
export type Model = Schemas["ModelView"];
export type Deployment = Schemas["DeploymentView"];

// Reporting, exports and audit.
export type LedgerRecord = Schemas["LedgerRecord"];
export type RequestRecord = Schemas["RequestRecord"];
export type UsageReport = Schemas["UsageReport"];
export type UsageBucket = Schemas["UsageBucket"];
export type AuditEvent = Schemas["AuditEvent"];
export type ExportJob = Schemas["ExportJob"];
export type ExportKind = ExportJob["kind"];
export type TimeBasis = Schemas["ReportPage_LedgerRecord_"]["time_basis"];

// Gateway publication and policies.
export type GatewayCluster = Schemas["ClusterView"];
export type ReleaseStatus = Schemas["ReleaseStatus"];
export type RoutePolicy = Schemas["RoutePolicyView"];
export type RouteCandidate = Schemas["Candidate"];
export type QuotaPolicy = Schemas["QuotaPolicyView"];

// Upstream copies of tenant content held for API keys, and owner purge requests.
export type UpstreamResource = Schemas["UpstreamResourceView"];
export type UpstreamResourceKind = UpstreamResource["kind"];
export type UpstreamResourceState = UpstreamResource["state"];
export type PurgeRequest = Schemas["PurgeRequestView"];

/** Shape shared by every cursor-paginated list response. */
export interface Page<T> {
  items: T[];
  next_cursor?: string | null;
}

export type PlatformRole = "platform_admin" | "platform_finance";
export type OrgRole = "org_owner" | "org_developer" | "org_finance";
export type Role = PlatformRole | OrgRole;
