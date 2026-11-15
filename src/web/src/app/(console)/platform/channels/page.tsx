"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { EllipsisIcon, HistoryIcon, PencilIcon, PlugIcon, PlusIcon } from "lucide-react";
import { useMemo, useState } from "react";
import { toast } from "sonner";

import { DataTable } from "@/components/data-table";
import { EmptyState, ErrorAlert } from "@/components/feedback";
import { Money } from "@/components/money";
import { LoadMoreFooter, PageContainer, PageHeader } from "@/components/page";
import { describeConfigSchema, redactConfiguration } from "@/components/platform-finance/config-form";
import { DetailList } from "@/components/platform-finance/field";
import { JsonDisclosure } from "@/components/platform-finance/json-view";
import { ConfirmWriteDialog } from "@/components/platform-finance/write-dialog";
import { StatusBadge, toneFor } from "@/components/status-badge";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "@/components/ui/sheet";
import { Skeleton } from "@/components/ui/skeleton";
import { useI18n } from "@/i18n/provider";
import { call, nexus } from "@/lib/api/client";
import { useCursorList } from "@/lib/api/hooks";
import type { components } from "@/lib/api/schema";
import { formatDateTime, shortId } from "@/lib/format";

import { CHANNELS_KEY, CreateChannelDialog, EditChannelDialog, useChannelDetail, type GatewayType } from "./channel-form";

type Channel = components["schemas"]["ChannelView"];
type ChannelVersion = components["schemas"]["ChannelVersionView"];

function useGatewayTypes() {
  return useQuery({
    queryKey: ["payment-gateway-types"],
    queryFn: async () => (await call(() => nexus.GET("/api/v1/payment-gateway-types"))).data.items,
    staleTime: 5 * 60_000,
  });
}

function Capabilities({ capabilities }: { capabilities: ChannelVersion["capabilities"] }) {
  const { t, maybeT } = useI18n();
  return (
    <DetailList
      items={[
        [
          t("pf.channels.capCurrencies"),
          Object.entries(capabilities.currencies)
            .map(([currency, decimals]) => t("pf.channels.capCurrency", { currency, decimals }))
            .join("，") || "—",
        ],
        [t("pf.channels.capQuery"), capabilities.supports_query ? t("common.yes") : t("common.no")],
        [t("pf.channels.capClose"), capabilities.supports_close ? t("common.yes") : t("common.no")],
        [
          t("pf.channels.capIdempotency"),
          `${maybeT(`pf.channels.idempotency.${capabilities.idempotency}`) ?? capabilities.idempotency}${
            capabilities.idempotency_seconds ? ` · ${t("pf.channels.seconds", { seconds: capabilities.idempotency_seconds })}` : ""
          }`,
        ],
        [t("pf.channels.capCheckout"), (capabilities.checkout_kinds ?? []).join(", ") || "—"],
        [
          t("pf.channels.capHosts"),
          capabilities.checkout_hosts.length ? (
            <span key="h" className="flex flex-wrap gap-1">
              {capabilities.checkout_hosts.map((host) => (
                <code key={host} className="rounded border bg-muted/40 px-1 font-mono text-xs">
                  {host}
                </code>
              ))}
            </span>
          ) : (
            "—"
          ),
        ],
      ]}
    />
  );
}

function RevokeDialog({
  channel,
  version,
  ifMatch,
  onReload,
  onClose,
}: {
  channel: Channel;
  version: ChannelVersion;
  ifMatch: string;
  onReload: () => void;
  onClose: () => void;
}) {
  const { t } = useI18n();
  const queryClient = useQueryClient();
  const current = version.id === channel.active_config_id;
  const blocked = current && channel.status !== "disabled";
  const revoke = useMutation({
    mutationFn: async () =>
      (
        await call(() =>
          nexus.POST("/api/v1/payment-channels/{channel_id}/versions/{config_id}/revoke", {
            params: { path: { channel_id: channel.id, config_id: version.id }, header: { "If-Match": ifMatch } },
          }),
        )
      ).data,
    onSuccess: () => {
      toast.success(t("pf.channels.revokedToast", { version: version.version }));
      void queryClient.invalidateQueries({ queryKey: CHANNELS_KEY });
      onClose();
    },
  });
  return (
    <ConfirmWriteDialog
      open
      onClose={onClose}
      title={t("pf.channels.revokeTitle", { version: version.version })}
      description={t("pf.channels.revokeDescription")}
      submitLabel={t("pf.channels.revoke")}
      destructive
      pending={revoke.isPending}
      error={revoke.error}
      onReload={() => {
        revoke.reset();
        onReload();
        onClose();
      }}
      disabled={blocked}
      onSubmit={() => revoke.mutate()}
    >
      <DetailList
        className="rounded-md border px-3 py-2.5"
        items={[
          [t("pf.channels.name"), channel.name],
          [t("pf.channels.configVersion"), `v${version.version} · ${shortId(version.id)}`],
          [t("pf.channels.keyVersion"), String(version.key_version)],
          [t("pf.common.createdAt"), `${formatDateTime(version.created_at)} ${t("common.utc")}`],
        ]}
      />
      {blocked ? (
        <p className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-900">{t("pf.channels.disableFirst")}</p>
      ) : current ? (
        <p className="text-sm text-muted-foreground">{t("pf.channels.revokeCurrent")}</p>
      ) : null}
    </ConfirmWriteDialog>
  );
}

function VersionsSheet({ channelId, types, onClose }: { channelId: string; types: GatewayType[]; onClose: () => void }) {
  const { t } = useI18n();
  const detail = useChannelDetail(channelId);
  const channel = detail.data?.data;
  const ifMatch = detail.data ? (detail.data.etag ?? `"${detail.data.data.version}"`) : null;
  const [revoking, setRevoking] = useState<ChannelVersion | null>(null);
  const type = types.find((item) => item.adapter_type === channel?.adapter_type);
  const spec = useMemo(() => (type ? describeConfigSchema(type.configuration_schema) : null), [type]);
  const versions = useCursorList<ChannelVersion>({
    queryKey: [...CHANNELS_KEY, "versions", channelId, "list"],
    fetchPage: async (cursor) =>
      (
        await call(() =>
          nexus.GET("/api/v1/payment-channels/{channel_id}/versions", {
            params: { path: { channel_id: channelId }, query: { limit: 20, cursor } },
          }),
        )
      ).data,
  });

  return (
    <Sheet open onOpenChange={(next) => !next && onClose()}>
      <SheetContent className="w-full gap-0 overflow-y-auto sm:max-w-[600px]" closeLabel={t("common.close")}>
        <SheetHeader className="border-b pr-12">
          <SheetTitle>{t("pf.channels.versionsTitle", { name: channel?.name ?? "…" })}</SheetTitle>
          <SheetDescription>{t("pf.channels.versionsDescription")}</SheetDescription>
        </SheetHeader>
        <div className="flex flex-col gap-3 p-4">
          {detail.error ? <ErrorAlert error={detail.error} onRetry={() => void detail.refetch()} /> : null}
          {versions.error && versions.items.length === 0 ? (
            <ErrorAlert error={versions.error} onRetry={() => void versions.refetch()} />
          ) : versions.isPending ? (
            <Skeleton className="h-40 w-full" />
          ) : (
            versions.items.map((version) => {
              const current = version.id === channel?.active_config_id;
              return (
                <article key={version.id} className="flex flex-col gap-3 rounded-lg border p-3">
                  <header className="flex flex-wrap items-center gap-2">
                    <span className="font-semibold">v{version.version}</span>
                    {current ? <StatusBadge tone="success">{t("pf.channels.current")}</StatusBadge> : null}
                    {version.revoked_at ? <StatusBadge tone="danger">{t("pf.channels.revoked")}</StatusBadge> : null}
                    <span className="text-xs text-muted-foreground tabular-nums">
                      {formatDateTime(version.created_at)} {t("common.utc")}
                    </span>
                    {!version.revoked_at && channel && ifMatch ? (
                      <Button size="sm" variant="outline" className="ml-auto" onClick={() => setRevoking(version)}>
                        {t("pf.channels.revoke")}
                      </Button>
                    ) : null}
                  </header>
                  <DetailList
                    items={[
                      [t("pf.channels.keyVersion"), String(version.key_version)],
                      [
                        t("pf.channels.verificationUntil"),
                        version.verification_valid_until ? `${formatDateTime(version.verification_valid_until)} ${t("common.utc")}` : "—",
                      ],
                      [t("pf.channels.revokedAt"), version.revoked_at ? `${formatDateTime(version.revoked_at)} ${t("common.utc")}` : "—"],
                    ]}
                  />
                  <Capabilities capabilities={version.capabilities} />
                  <JsonDisclosure
                    title={t("pf.channels.publicConfiguration")}
                    value={redactConfiguration(spec, version.public_configuration)}
                  />
                </article>
              );
            })
          )}
          {versions.hasNextPage ? (
            <Button
              variant="outline"
              size="sm"
              className="self-start"
              disabled={versions.isFetchingNextPage}
              onClick={() => void versions.fetchNextPage()}
            >
              {versions.isFetchingNextPage ? t("common.loading") : t("common.loadMore")}
            </Button>
          ) : null}
        </div>
        {revoking && channel && ifMatch ? (
          <RevokeDialog
            channel={channel}
            version={revoking}
            ifMatch={ifMatch}
            onReload={() => void detail.refetch()}
            onClose={() => setRevoking(null)}
          />
        ) : null}
      </SheetContent>
    </Sheet>
  );
}

export default function ChannelsPage() {
  const { t, maybeT } = useI18n();
  const types = useGatewayTypes();
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<string | null>(null);
  const [versionsOf, setVersionsOf] = useState<string | null>(null);
  const typeList = useMemo(() => types.data ?? [], [types.data]);

  const channels = useCursorList<Channel>({
    queryKey: [...CHANNELS_KEY, "list"],
    fetchPage: async (cursor) =>
      (await call(() => nexus.GET("/api/v1/payment-channels", { params: { query: { limit: 50, cursor } } }))).data,
  });

  const columns = useMemo<ColumnDef<Channel, unknown>[]>(
    () => [
      {
        header: t("pf.channels.name"),
        accessorKey: "name",
        cell: ({ row }) => (
          <span className="flex flex-col">
            <span className="font-medium">{row.original.name}</span>
            <span className="font-mono text-xs text-muted-foreground">{row.original.adapter_type}</span>
          </span>
        ),
      },
      {
        header: t("pf.channels.merchantId"),
        accessorKey: "stable_merchant_id",
        cell: ({ row }) => <code className="font-mono text-xs">{row.original.stable_merchant_id}</code>,
      },
      {
        header: t("pf.channels.environment"),
        accessorKey: "environment",
        cell: ({ row }) => (
          <StatusBadge tone={row.original.environment === "live" ? "info" : "warning"}>
            {maybeT(`pf.channels.environments.${row.original.environment}`) ?? row.original.environment}
          </StatusBadge>
        ),
      },
      {
        id: "limits",
        header: t("pf.channels.limits"),
        meta: { align: "right" },
        cell: ({ row }) => (
          <span className="inline-flex items-center gap-1 tabular-nums">
            <span className="text-xs text-muted-foreground">{row.original.currency}</span>
            <Money value={row.original.min_amount} />
            <span className="text-muted-foreground">–</span>
            <Money value={row.original.max_amount} />
          </span>
        ),
      },
      {
        header: t("pf.common.status"),
        accessorKey: "status",
        cell: ({ row }) => (
          <StatusBadge tone={toneFor(row.original.status)}>
            {maybeT(`pf.common.states.${row.original.status}`) ?? row.original.status}
          </StatusBadge>
        ),
      },
      {
        header: t("pf.channels.activeConfig"),
        accessorKey: "active_config_id",
        cell: ({ row }) => (
          <code className="font-mono text-xs text-muted-foreground" title={row.original.active_config_id}>
            {shortId(row.original.active_config_id)}
          </code>
        ),
      },
      {
        header: t("pf.common.createdAt"),
        accessorKey: "created_at",
        cell: ({ row }) => <span className="text-muted-foreground tabular-nums">{formatDateTime(row.original.created_at, { seconds: false })}</span>,
      },
      {
        id: "actions",
        header: () => <span className="sr-only">{t("pf.common.actions")}</span>,
        meta: { align: "right" },
        cell: ({ row }) => (
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button variant="ghost" size="icon-sm" aria-label={t("common.more")}>
                <EllipsisIcon />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              <DropdownMenuItem onSelect={() => setEditing(row.original.id)}>
                <PencilIcon />
                {t("pf.common.edit")}
              </DropdownMenuItem>
              <DropdownMenuItem onSelect={() => setVersionsOf(row.original.id)}>
                <HistoryIcon />
                {t("pf.channels.versions")}
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        ),
      },
    ],
    [t, maybeT],
  );

  return (
    <PageContainer>
      <PageHeader
        title={t("pf.channels.title")}
        description={t("pf.channels.subtitle")}
        actions={
          <Button onClick={() => setCreating(true)} disabled={!types.data}>
            <PlusIcon />
            {t("pf.channels.create")}
          </Button>
        }
      />
      {types.error ? <ErrorAlert error={types.error} onRetry={() => void types.refetch()} /> : null}
      {types.data ? (
        <p className="text-xs text-muted-foreground">
          {t("pf.channels.installed")}{" "}
          {types.data.map((type) => (
            <code key={type.adapter_type} className="mr-1.5 rounded border bg-muted/40 px-1 font-mono">
              {type.adapter_type}
              {type.test_only ? `（${t("pf.channels.testOnly")}）` : ""}
            </code>
          ))}
        </p>
      ) : null}
      <DataTable
        columns={columns}
        data={channels.items}
        loading={channels.isPending}
        error={channels.error}
        onRetry={() => void channels.refetch()}
        getRowId={(row) => row.id}
        minWidth={980}
        rowClassName={(row) => (row.status === "disabled" ? "text-muted-foreground" : undefined)}
        empty={
          <EmptyState
            icon={<PlugIcon className="size-5" />}
            title={t("pf.channels.empty")}
            description={t("pf.channels.emptyBody")}
          />
        }
        footer={
          channels.items.length > 0 ? (
            <LoadMoreFooter
              count={channels.items.length}
              hasNext={Boolean(channels.hasNextPage)}
              loading={channels.isFetchingNextPage}
              onLoadMore={() => void channels.fetchNextPage()}
            />
          ) : null
        }
      />
      <CreateChannelDialog open={creating} onOpenChange={setCreating} types={typeList} />
      {editing ? <EditChannelDialog channelId={editing} types={typeList} onClose={() => setEditing(null)} /> : null}
      {versionsOf ? <VersionsSheet channelId={versionsOf} types={typeList} onClose={() => setVersionsOf(null)} /> : null}
    </PageContainer>
  );
}
