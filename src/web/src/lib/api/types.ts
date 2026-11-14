import type { components } from "./schema";

type Schemas = components["schemas"];

// Generated from the OpenAPI contract.
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

export interface Page<T> {
  items: T[];
  next_cursor?: string | null;
}

export type PlatformRole = "platform_admin" | "platform_finance";
export type OrgRole = "org_owner" | "org_developer" | "org_finance";
export type Role = PlatformRole | OrgRole;

// ---------------------------------------------------------------------------------------
// Hand-written types for endpoints whose responses the contract does not describe yet
// (reporting, exports, audit). Shapes were taken from the running API; replace them with
// generated types once the OpenAPI contract declares response models.
// ---------------------------------------------------------------------------------------

export type TimeBasis = "occurred_at" | "posted_at";

export interface ReportPage<T> extends Page<T> {
  time_basis: TimeBasis;
}

export interface LedgerEntry {
  id: string;
  organization_id: string;
  account_id: string;
  sequence: number;
  currency: string;
  signed_amount: string;
  balance_after: string;
  kind: string;
  source_type: string;
  source_id: string;
  reverses_entry_id: string | null;
  actor_id: string | null;
  posted_at: string;
  occurred_at: string;
  project_id: string | null;
  api_key_id: string | null;
  model_id: string | null;
}

export interface RequestRow {
  id: string;
  organization_id: string;
  project_id: string;
  api_key_id: string;
  model_id: string;
  occurred_at: string;
  posted_at: string | null;
  state: string;
  reservation_state: string;
  currency: string;
  reserved_amount: string | null;
  charged_amount: string | null;
  refunded_amount: string | null;
}

export interface UsageRow {
  /** Group value: organization/project/key/model id, or "YYYY-MM-DD 00:00:00" for day. */
  group: string;
  requests: number;
  held_requests: number;
  quantities: Record<string, number>;
  held_amount: string;
  charged_amount: string;
  refund_amount: string;
  net_charge: string;
  confirmed_cost?: string;
  margin?: string;
  provisional?: boolean;
}

export interface UsageReport {
  items: UsageRow[];
  watermark: { latest_source_at: string | null; projected_at: string | null; pending_requests: number };
  organization_id: string;
  group_by: "organization" | "project" | "key" | "model" | "day";
  currency: string;
}

export interface AuditEvent {
  id: string;
  actor_id: string | null;
  organization_id: string | null;
  action: string;
  resource_id: string | null;
  created_at: string;
  reason: string | null;
  decision_id: string | null;
  module: string;
}

export type ExportKind = "requests" | "charges" | "ledger" | "attempt_costs";

export interface ExportJob {
  id: string;
  organization_id: string;
  actor_id: string;
  kind: ExportKind;
  filters: Record<string, unknown>;
  state: "queued" | "running" | "ready" | "failed" | "expired" | string;
  row_count: number | null;
  error_code: string | null;
  created_at: string;
  completed_at: string | null;
  expires_at: string;
}
