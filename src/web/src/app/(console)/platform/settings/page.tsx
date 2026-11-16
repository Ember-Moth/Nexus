"use client";

import { useState, type ReactNode } from "react";

import { EmptyState, ErrorAlert } from "@/components/feedback";
import { PageContainer, PageHeader } from "@/components/page";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useT } from "@/i18n/provider";
import { call, nexus } from "@/lib/api/client";
import { useCursorList } from "@/lib/api/hooks";
import type { components } from "@/lib/api/schema";
import type { GatewayCluster } from "@/lib/api/types";
import { formatDateTime } from "@/lib/format";

import { PolicyCard, type PolicyGroup } from "./policy-form";

type Schemas = components["schemas"];
type AuthPolicy = Schemas["AuthPolicy"];
type SiteInfo = Schemas["SiteInfo"];
type SitePolicy = Schemas["SitePolicyView"];
type BillingPolicy = Schemas["BillingPolicy"];
type ReportingPolicy = Schemas["ReportingPolicy"];
type ResourceQuotaDefaults = Schemas["ResourceQuotaDefaults"];
type ClusterPolicy = Schemas["ClusterPolicy"];
type ClusterOverrides = Schemas["ClusterOverrides"];
type ClusterPolicyView = Schemas["ClusterPolicyView"];

const INT_MAX = 2_147_483_647;
const COUNT_MAX = 2_000_000_000;

const SITE: PolicyGroup[] = [
  {
    fields: [
      { path: "site_name", label: "st.site.fields.site_name", hint: "st.site.hints.site_name", kind: "text", max: 100 },
      {
        path: "console_url",
        label: "st.site.fields.console_url",
        hint: "st.site.hints.console_url",
        kind: "text",
        max: 2048,
        optional: true,
      },
      { path: "contact_email", label: "st.site.fields.contact_email", kind: "text", max: 254, optional: true },
    ],
  },
];

const IDENTITY: PolicyGroup[] = [
  {
    title: "st.identity.signIn",
    fields: [
      { path: "sign_in.window_seconds", label: "st.identity.fields.sign_in.window_seconds", kind: "int", min: 1, max: 3600 },
      { path: "sign_in.per_account", label: "st.identity.fields.sign_in.per_account", kind: "int", min: 1, max: 1000 },
      {
        path: "sign_in.per_ip",
        label: "st.identity.fields.sign_in.per_ip",
        hint: "st.identity.hints.sign_in.per_ip",
        kind: "int",
        min: 1,
        max: 10000,
      },
    ],
  },
  {
    title: "st.identity.mail",
    fields: [
      { path: "mail.window_seconds", label: "st.identity.fields.mail.window_seconds", kind: "int", min: 60, max: 86400 },
      {
        path: "mail.per_recipient",
        label: "st.identity.fields.mail.per_recipient",
        hint: "st.identity.hints.mail.per_recipient",
        kind: "int",
        min: 1,
        max: 1000,
      },
      { path: "mail.per_recipient_total", label: "st.identity.fields.mail.per_recipient_total", kind: "int", min: 1, max: 10000 },
      { path: "mail.per_account", label: "st.identity.fields.mail.per_account", kind: "int", min: 1, max: 10000 },
      { path: "mail.per_ip", label: "st.identity.fields.mail.per_ip", kind: "int", min: 1, max: 100000 },
      { path: "mail.global_limit", label: "st.identity.fields.mail.global_limit", kind: "int", min: 1, max: 10_000_000 },
      {
        path: "mail.queue_ttl_seconds",
        label: "st.identity.fields.mail.queue_ttl_seconds",
        hint: "st.identity.hints.mail.queue_ttl_seconds",
        kind: "int",
        min: 60,
        max: 86400,
      },
    ],
  },
  {
    title: "st.identity.sessions",
    fields: [
      { path: "sessions.session_ttl_seconds", label: "st.identity.fields.sessions.session_ttl_seconds", kind: "int", min: 60, max: 86400 },
      {
        path: "sessions.password_setup_ttl_seconds",
        label: "st.identity.fields.sessions.password_setup_ttl_seconds",
        kind: "int",
        min: 60,
        max: 86400,
      },
    ],
  },
  {
    title: "st.identity.other",
    fields: [
      {
        path: "reserved_logins",
        label: "st.identity.fields.reserved_logins",
        hint: "st.identity.hints.reserved_logins",
        kind: "lines",
      },
      {
        path: "platform_email_recovery",
        label: "st.identity.fields.platform_email_recovery",
        hint: "st.identity.hints.platform_email_recovery",
        kind: "bool",
      },
    ],
  },
];

const BILLING: PolicyGroup[] = [
  {
    title: "st.billing.currencyGroup",
    fields: [
      { path: "currency", label: "st.billing.currency", hint: "st.billing.currencyHint", kind: "currency" },
    ],
  },
  {
    title: "st.billing.storage",
    fields: [
      { path: "vector_storage.prepaid_days", label: "st.billing.fields.vector_storage.prepaid_days", kind: "int", min: 1, max: 31 },
      {
        path: "vector_storage.lapse_policy",
        label: "st.billing.fields.vector_storage.lapse_policy",
        kind: "select",
        options: [
          { value: "delete", label: "st.billing.lapse.delete" },
          { value: "suspend", label: "st.billing.lapse.suspend" },
        ],
      },
    ],
  },
  {
    title: "st.billing.payments",
    fields: [
      {
        path: "payments.requests_per_merchant_per_minute",
        label: "st.billing.fields.payments.requests_per_merchant_per_minute",
        kind: "int",
        min: 1,
        max: 10000,
      },
      {
        path: "payments.notifications_per_channel_per_minute",
        label: "st.billing.fields.payments.notifications_per_channel_per_minute",
        kind: "int",
        min: 1,
        max: 100000,
      },
      {
        path: "payments.notifications_per_ip_per_minute",
        label: "st.billing.fields.payments.notifications_per_ip_per_minute",
        kind: "int",
        min: 1,
        max: 10000,
      },
    ],
  },
];

const REPORTING: PolicyGroup[] = [
  {
    fields: [
      { path: "exports.per_org_minute", label: "st.reporting.fields.exports.per_org_minute", kind: "int", min: 1, max: 1000 },
      { path: "exports.ttl_seconds", label: "st.reporting.fields.exports.ttl_seconds", kind: "int", min: 60, max: 2_592_000 },
      { path: "exports.max_rows", label: "st.reporting.fields.exports.max_rows", kind: "int", min: 1, max: 100000 },
    ],
  },
];

const QUOTAS: PolicyGroup[] = [
  {
    fields: [
      { path: "resources_per_key", label: "st.quotas.fields.resources_per_key", kind: "int", min: 1, max: COUNT_MAX },
      { path: "resources_per_organization", label: "st.quotas.fields.resources_per_organization", kind: "int", min: 1, max: COUNT_MAX },
      { path: "vector_stores_per_key", label: "st.quotas.fields.vector_stores_per_key", kind: "int", min: 1, max: COUNT_MAX },
      {
        path: "vector_stores_per_organization",
        label: "st.quotas.fields.vector_stores_per_organization",
        kind: "int",
        min: 1,
        max: COUNT_MAX,
      },
    ],
  },
];

const CLUSTER: PolicyGroup[] = [
  {
    title: "st.clusters.relay",
    fields: [
      {
        path: "relay.max_attempts",
        label: "st.clusters.fields.relay.max_attempts",
        hint: "st.clusters.hints.relay.max_attempts",
        kind: "int",
        min: 1,
        max: 4,
      },
      { path: "relay.failover_budget_ms", label: "st.clusters.fields.relay.failover_budget_ms", kind: "int", min: 100, max: 60000 },
      { path: "relay.purge_grace_seconds", label: "st.clusters.fields.relay.purge_grace_seconds", kind: "int", min: 0, max: INT_MAX },
      { path: "relay.stream_lifetime_seconds", label: "st.clusters.fields.relay.stream_lifetime_seconds", kind: "int", min: 1, max: INT_MAX },
    ],
  },
  {
    title: "st.clusters.autoPublish",
    fields: [
      { path: "auto_publish.quiet_seconds", label: "st.clusters.fields.auto_publish.quiet_seconds", kind: "float", min: 0, max: 300 },
      {
        path: "auto_publish.max_delay_seconds",
        label: "st.clusters.fields.auto_publish.max_delay_seconds",
        hint: "st.clusters.hints.auto_publish.max_delay_seconds",
        kind: "float",
        min: 0.001,
        max: 3600,
      },
    ],
  },
];

// The same fields as overrides: empty follows the platform default.
const OVERRIDES: PolicyGroup[] = CLUSTER.map((group) => ({
  ...group,
  fields: group.fields.map((field) => ({ ...field, optional: true })),
}));

function Note({ children }: { children: ReactNode }) {
  return <p className="text-xs text-muted-foreground">{children}</p>;
}

/**
 * Platform settings by module. Each card is one policy resource with its own version and audit
 * trail; API and worker processes apply a change within seconds, gateway nodes from the next
 * release of their cluster.
 */
export default function PlatformSettingsPage() {
  const t = useT();
  return (
    <PageContainer>
      <PageHeader title={t("st.title")} description={t("st.description")} />
      <Tabs defaultValue="site" className="gap-4">
        <TabsList>
          <TabsTrigger value="site">{t("st.tabs.site")}</TabsTrigger>
          <TabsTrigger value="identity">{t("st.tabs.identity")}</TabsTrigger>
          <TabsTrigger value="billing">{t("st.tabs.billing")}</TabsTrigger>
          <TabsTrigger value="reporting">{t("st.tabs.reporting")}</TabsTrigger>
          <TabsTrigger value="quotas">{t("st.tabs.quotas")}</TabsTrigger>
          <TabsTrigger value="clusters">{t("st.tabs.clusters")}</TabsTrigger>
        </TabsList>
        <TabsContent value="site">
          <PolicyCard<SitePolicy>
            title={t("st.site.title")}
            description={t("st.site.description")}
            queryKey={["settings", "site"]}
            load={() => call(() => nexus.GET("/api/v1/settings/site"))}
            save={(body, etag) =>
              call(() =>
                nexus.PUT("/api/v1/settings/site", {
                  params: { header: { "If-Match": etag } },
                  body: body as SiteInfo,
                }),
              )
            }
            groups={SITE}
            facts={(policy) => (
              <Note>
                {policy.setup_completed_at
                  ? t("st.site.completed", { at: formatDateTime(policy.setup_completed_at) })
                  : t("st.site.pending")}
              </Note>
            )}
          />
        </TabsContent>
        <TabsContent value="identity">
          <PolicyCard
            title={t("st.identity.title")}
            description={t("st.identity.description")}
            // Shared with the switches card on the users page: one resource, one version.
            queryKey={["settings", "authentication"]}
            load={() => call(() => nexus.GET("/api/v1/settings/authentication"))}
            save={(body, etag) =>
              call(() =>
                nexus.PUT("/api/v1/settings/authentication", {
                  params: { header: { "If-Match": etag } },
                  body: body as AuthPolicy,
                }),
              )
            }
            groups={IDENTITY}
            footer={<Note>{t("st.identity.switches")}</Note>}
          />
        </TabsContent>
        <TabsContent value="billing">
          <BillingCard />
        </TabsContent>
        <TabsContent value="reporting">
          <PolicyCard
            title={t("st.reporting.title")}
            description={t("st.reporting.description")}
            queryKey={["settings", "reporting"]}
            load={() => call(() => nexus.GET("/api/v1/settings/reporting"))}
            save={(body, etag) =>
              call(() =>
                nexus.PUT("/api/v1/settings/reporting", {
                  params: { header: { "If-Match": etag } },
                  body: body as ReportingPolicy,
                }),
              )
            }
            groups={REPORTING}
          />
        </TabsContent>
        <TabsContent value="quotas">
          <PolicyCard
            title={t("st.quotas.title")}
            description={t("st.quotas.description")}
            queryKey={["settings", "resource-quotas"]}
            load={() => call(() => nexus.GET("/api/v1/settings/resource-quotas"))}
            save={(body, etag) =>
              call(() =>
                nexus.PUT("/api/v1/settings/resource-quotas", {
                  params: { header: { "If-Match": etag } },
                  body: body as ResourceQuotaDefaults,
                }),
              )
            }
            groups={QUOTAS}
            footer={<Note>{t("st.quotas.capability")}</Note>}
          />
        </TabsContent>
        <TabsContent value="clusters">
          <ClusterPolicies />
        </TabsContent>
      </Tabs>
    </PageContainer>
  );
}

function BillingCard() {
  const t = useT();
  return (
    <PolicyCard
      title={t("st.billing.title")}
      description={t("st.billing.description")}
      queryKey={["settings", "billing"]}
      load={() => call(() => nexus.GET("/api/v1/settings/billing"))}
      save={(body, etag) =>
        call(() =>
          nexus.PUT("/api/v1/settings/billing", {
            params: { header: { "If-Match": etag } },
            body: body as BillingPolicy,
          }),
        )
      }
      groups={BILLING}
    />
  );
}

function ClusterPolicies() {
  const t = useT();
  const clusters = useCursorList<GatewayCluster>({
    queryKey: ["gateway-clusters", "settings-options"],
    fetchPage: async (cursor) =>
      (await call(() => nexus.GET("/api/v1/gateway-clusters", { params: { query: { limit: 100, cursor } } }))).data,
  });
  const [selected, setSelected] = useState<string | null>(null);
  const items = clusters.items;
  const clusterId = selected ?? items[0]?.id ?? null;

  return (
    <div className="grid gap-4">
      <PolicyCard
        title={t("st.clusters.defaultsTitle")}
        description={t("st.clusters.defaultsDescription")}
        queryKey={["settings", "cluster-defaults"]}
        load={() => call(() => nexus.GET("/api/v1/settings/cluster-defaults"))}
        save={(body, etag) =>
          call(() =>
            nexus.PUT("/api/v1/settings/cluster-defaults", {
              params: { header: { "If-Match": etag } },
              body: body as ClusterPolicy,
            }),
          )
        }
        groups={CLUSTER}
        footer={<Note>{t("st.clusters.capability")}</Note>}
      />
      {clusters.error && items.length === 0 ? (
        <ErrorAlert error={clusters.error} onRetry={() => void clusters.refetch()} />
      ) : clusters.isLoading ? (
        <Skeleton className="h-40" />
      ) : !clusterId ? (
        <EmptyState title={t("st.clusters.empty")} />
      ) : (
        <div className="grid gap-3">
          <div className="flex flex-wrap items-center gap-3">
            <span className="text-sm text-muted-foreground">{t("st.clusters.select")}</span>
            <Select value={clusterId} onValueChange={setSelected}>
              <SelectTrigger className="w-[280px]" aria-label={t("st.clusters.select")}>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {items.map((cluster) => (
                  <SelectItem key={cluster.id} value={cluster.id}>
                    {cluster.name} <span className="text-muted-foreground">· {cluster.region}</span>
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <PolicyCard<ClusterPolicyView>
            key={clusterId}
            title={t("st.clusters.title")}
            description={t("st.clusters.description")}
            queryKey={["gateway-clusters", clusterId, "policy"]}
            load={() =>
              call(() =>
                nexus.GET("/api/v1/gateway-clusters/{cluster_id}/policy", { params: { path: { cluster_id: clusterId } } }),
              )
            }
            save={(body, etag) =>
              call(() =>
                nexus.PUT("/api/v1/gateway-clusters/{cluster_id}/policy", {
                  params: { path: { cluster_id: clusterId }, header: { "If-Match": etag } },
                  body: body as ClusterOverrides,
                }),
              )
            }
            groups={OVERRIDES}
            editable={(view) => view.overrides as unknown as Record<string, unknown>}
            placeholder={(view, path) => {
              const [group, name] = path.split(".") as ["relay" | "auto_publish", string];
              const value = (view.effective[group] as Record<string, number>)[name];
              return t("st.clusters.follows", { value: String(value) });
            }}
          />
        </div>
      )}
    </div>
  );
}
