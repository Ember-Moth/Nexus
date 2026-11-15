"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import {
  ArrowLeftIcon,
  ArrowRightIcon,
  BanIcon,
  CheckIcon,
  GitBranchIcon,
  KeyRoundIcon,
  PencilIcon,
  PlusIcon,
  PowerIcon,
} from "lucide-react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useMemo, useState } from "react";
import { toast } from "sonner";

import { DataTable } from "@/components/data-table";
import { ConflictAlert, EmptyState, Spinner } from "@/components/feedback";
import { LoadMoreFooter, PageContainer, PageHeader } from "@/components/page";
import { CatalogStatusBadge, ConfiguredBadge, ProtocolBadge } from "@/components/platform-catalog/badges";
import { PcErrorAlert } from "@/components/platform-catalog/errors";
import {
  etagOf,
  invalidateCatalog,
  useCredentials,
  useDeployments,
  useModels,
  useUpstreamDetail,
} from "@/components/platform-catalog/queries";
import type { CatalogDeployment, Credential, Upstream } from "@/components/platform-catalog/types";
import { StatusBadge } from "@/components/status-badge";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { useT } from "@/i18n/provider";
import { call, nexus } from "@/lib/api/client";
import { isConflict } from "@/lib/api/errors";
import { formatDate, formatDateTime, shortId } from "@/lib/format";
import { cn } from "@/lib/utils";

import { DeploymentDialog } from "../../deployments/deployment-dialog";
import { UpstreamDialog } from "../upstream-dialog";
import { CreateCredentialDialog, RevokeCredentialDialog } from "./credential-dialogs";

/** Disable or re-enable the upstream with the detail ETag; disabling stops its deployments. */
function StatusDialog({
  upstream,
  etag,
  onReload,
  onClose,
}: {
  upstream: Upstream;
  etag: string | null;
  onReload: () => void;
  onClose: () => void;
}) {
  const t = useT();
  const queryClient = useQueryClient();
  const next = upstream.status === "active" ? "disabled" : "active";
  const save = useMutation({
    mutationFn: async () =>
      call(() =>
        nexus.PATCH("/api/v1/upstreams/{upstream_id}", {
          params: { path: { upstream_id: upstream.id }, header: { "If-Match": etag ?? `"${upstream.version}"` } },
          body: { status: next },
        }),
      ),
    onSuccess: () => {
      toast.success(t("pc.upstreams.statusDialog.done"));
      void invalidateCatalog(queryClient);
      onClose();
    },
  });
  const conflict = isConflict(save.error);
  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent role="alertdialog" className="sm:max-w-[440px]">
        <DialogHeader>
          <DialogTitle>
            {next === "disabled" ? t("pc.upstreams.statusDialog.disableTitle") : t("pc.upstreams.statusDialog.enableTitle")}
          </DialogTitle>
          <DialogDescription>
            {next === "disabled" ? t("pc.upstreams.statusDialog.disableBody") : t("pc.upstreams.statusDialog.enableBody")}
          </DialogDescription>
        </DialogHeader>
        {conflict ? (
          <ConflictAlert
            onReload={() => {
              save.reset();
              onReload();
              onClose();
            }}
          />
        ) : save.error ? (
          <PcErrorAlert error={save.error} />
        ) : null}
        <DialogFooter>
          <Button type="button" variant="outline" onClick={onClose}>
            {t("common.cancel")}
          </Button>
          <Button
            type="button"
            variant={next === "disabled" ? "destructive" : "default"}
            disabled={save.isPending || conflict}
            onClick={() => save.mutate()}
          >
            {save.isPending ? <Spinner /> : null}
            {next === "disabled" ? t("pc.upstreams.disable") : t("pc.upstreams.enable")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function RotationBanner({ remaining }: { remaining: number }) {
  const t = useT();
  const steps = [
    { label: t("pc.credentials.rotation.step1"), done: true },
    {
      label: remaining > 0 ? t("pc.credentials.rotation.step2", { count: remaining }) : t("pc.credentials.rotation.step2Done"),
      done: remaining === 0,
    },
    { label: t("pc.credentials.rotation.step3"), done: false },
  ];
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-2 rounded-md border border-blue-200 bg-blue-50 px-3 py-2.5 text-[13px] text-blue-900">
      <span className="font-medium">{t("pc.credentials.rotation.title")}</span>
      {steps.map((step, index) => (
        <span key={index} className="flex items-center gap-2">
          {index > 0 ? <ArrowRightIcon className="size-3.5 text-blue-700/70" aria-hidden /> : null}
          <span
            className={cn(
              "flex size-5 items-center justify-center rounded-full border text-[11px] font-semibold",
              step.done ? "border-blue-700 bg-blue-700 text-white" : "border-blue-300 bg-white",
            )}
          >
            {step.done ? <CheckIcon className="size-3" aria-hidden /> : index + 1}
          </span>
          {step.label}
        </span>
      ))}
    </div>
  );
}

type DetailDialog =
  | { kind: "edit" }
  | { kind: "status" }
  | { kind: "credential" }
  | { kind: "revoke"; credential: Credential }
  | { kind: "deployment"; deployment: CatalogDeployment | null };

export default function UpstreamDetailPage() {
  const t = useT();
  const { upstreamId } = useParams<{ upstreamId: string }>();
  const detail = useUpstreamDetail(upstreamId);
  const upstream = detail.data?.data;
  const credentials = useCredentials(upstreamId, 50);
  const models = useModels();
  const deployments = useDeployments({ upstreamId }, { scope: "by-upstream" });
  const { hasNextPage, isFetchingNextPage, fetchNextPage } = deployments;
  useEffect(() => {
    // Reference counts need every deployment of the upstream, not just the first page.
    if (hasNextPage && !isFetchingNextPage) void fetchNextPage();
  }, [hasNextPage, isFetchingNextPage, fetchNextPage]);

  const [dialog, setDialog] = useState<DetailDialog | null>(null);
  const close = () => setDialog(null);

  const usage = useMemo(() => {
    const map = new Map<string, CatalogDeployment[]>();
    for (const deployment of deployments.items) {
      if (deployment.status === "archived") continue;
      map.set(deployment.credential_id, [...(map.get(deployment.credential_id) ?? []), deployment]);
    }
    return map;
  }, [deployments.items]);
  const usageKnown = !deployments.error && !deployments.isPending && !hasNextPage;

  // Rotation: several active credentials; the newest is the target, older ones are to retire.
  const rotation = useMemo(() => {
    const active = credentials.items
      .filter((c) => c.status === "active")
      .sort((a, b) => Date.parse(b.created_at) - Date.parse(a.created_at));
    const older = new Set(active.slice(1).map((c) => c.id));
    const remaining = [...older].reduce((sum, id) => sum + (usage.get(id)?.length ?? 0), 0);
    return { inProgress: active.length > 1, older, remaining };
  }, [credentials.items, usage]);

  const credentialName = useMemo(() => new Map(credentials.items.map((c) => [c.id, c.name])), [credentials.items]);
  const modelName = useMemo(() => new Map(models.items.map((m) => [m.id, m.public_name])), [models.items]);

  const credentialColumns = useMemo<ColumnDef<Credential, unknown>[]>(
    () => [
      {
        header: t("pc.credentials.columns.name"),
        accessorKey: "name",
        cell: ({ row }) => <span className="font-medium">{row.original.name}</span>,
      },
      {
        header: t("pc.credentials.columns.keyVersion"),
        accessorKey: "key_version",
        cell: ({ row }) => <span className="tabular-nums text-muted-foreground">v{row.original.key_version}</span>,
      },
      {
        header: t("pc.credentials.columns.status"),
        accessorKey: "status",
        cell: ({ row }) => (
          <span title={row.original.revoked_at ? `${formatDateTime(row.original.revoked_at)} UTC` : undefined}>
            <StatusBadge tone={row.original.status === "active" ? "success" : "danger"}>
              {t(`pc.credentials.status.${row.original.status}`)}
            </StatusBadge>
          </span>
        ),
      },
      {
        id: "usedBy",
        header: t("pc.credentials.columns.usedBy"),
        cell: ({ row }) => {
          if (!usageKnown) {
            return deployments.error ? (
              <span className="text-xs text-amber-700">{t("pc.credentials.usageUnavailable")}</span>
            ) : (
              <Spinner className="text-muted-foreground" />
            );
          }
          const users = usage.get(row.original.id) ?? [];
          if (users.length === 0) return <span className="text-muted-foreground">{t("pc.credentials.unused")}</span>;
          const switching = rotation.older.has(row.original.id);
          return (
            <span title={users.map((d) => d.name).join("\n")} className={switching ? "text-amber-800" : undefined}>
              {switching
                ? t("pc.credentials.refsSwitch", { count: users.length })
                : t("pc.credentials.refs", { count: users.length })}
            </span>
          );
        },
      },
      {
        header: t("pc.credentials.columns.created"),
        accessorKey: "created_at",
        cell: ({ row }) => <span className="text-muted-foreground tabular-nums">{formatDate(row.original.created_at)}</span>,
      },
      {
        id: "actions",
        header: () => <span className="sr-only">{t("pc.credentials.columns.actions")}</span>,
        meta: { align: "right" },
        cell: ({ row }) =>
          row.original.status === "active" ? (
            <Button variant="outline" size="sm" onClick={() => setDialog({ kind: "revoke", credential: row.original })}>
              <BanIcon />
              {t("pc.credentials.revoke")}
            </Button>
          ) : null,
      },
    ],
    [t, usage, usageKnown, deployments.error, rotation],
  );

  const deploymentColumns = useMemo<ColumnDef<CatalogDeployment, unknown>[]>(
    () => [
      {
        header: t("pc.upstreams.deployments.columns.name"),
        accessorKey: "name",
        cell: ({ row }) => (
          <button
            type="button"
            className="font-medium hover:underline"
            onClick={() => setDialog({ kind: "deployment", deployment: row.original })}
          >
            {row.original.name}
          </button>
        ),
      },
      {
        id: "model",
        header: t("pc.upstreams.deployments.columns.model"),
        cell: ({ row }) => (
          <span className="font-mono text-[12.5px]">{modelName.get(row.original.model_id) ?? shortId(row.original.model_id)}</span>
        ),
      },
      {
        header: t("pc.upstreams.deployments.columns.upstreamModel"),
        accessorKey: "upstream_model",
        cell: ({ row }) => (
          <code className="block max-w-[220px] truncate font-mono text-[12.5px]" title={row.original.upstream_model}>
            {row.original.upstream_model}
          </code>
        ),
      },
      {
        header: t("pc.upstreams.deployments.columns.region"),
        accessorKey: "region",
        cell: ({ row }) => <span className="text-muted-foreground">{row.original.region}</span>,
      },
      {
        id: "credential",
        header: t("pc.upstreams.deployments.columns.credential"),
        cell: ({ row }) => (
          <span className="flex items-center gap-2">
            {credentialName.get(row.original.credential_id) ?? shortId(row.original.credential_id)}
            {rotation.older.has(row.original.credential_id) && row.original.status !== "archived" ? (
              <Badge variant="outline" className="border-amber-300 bg-amber-50 font-normal text-amber-800">
                {t("pc.upstreams.deployments.pending")}
              </Badge>
            ) : null}
          </span>
        ),
      },
      {
        header: t("pc.upstreams.deployments.columns.status"),
        accessorKey: "status",
        cell: ({ row }) => <CatalogStatusBadge status={row.original.status} />,
      },
      {
        id: "configured",
        header: t("pc.upstreams.deployments.columns.configured"),
        cell: ({ row }) => <ConfiguredBadge configured={row.original.configured} />,
      },
    ],
    [t, modelName, credentialName, rotation],
  );

  return (
    <PageContainer>
      <Button variant="ghost" size="sm" className="w-fit" asChild>
        <Link href="/platform/upstreams">
          <ArrowLeftIcon />
          {t("pc.upstreams.back")}
        </Link>
      </Button>
      {detail.error && !upstream ? (
        <PcErrorAlert error={detail.error} title={t("pc.upstreams.notFound")} onRetry={() => void detail.refetch()} />
      ) : null}
      {upstream ? (
        <>
          <PageHeader
            title={upstream.name}
            badge={
              <span className="flex items-center gap-2">
                <CatalogStatusBadge status={upstream.status} />
                <ProtocolBadge protocol={upstream.protocol_family} />
              </span>
            }
            description={
              <span className="flex flex-col gap-1.5">
                <code className="font-mono text-[13px] break-all text-foreground">{upstream.base_url}</code>
                <span>{t("pc.upstreams.detailNote")}</span>
              </span>
            }
            actions={
              <>
                <Button variant="outline" onClick={() => setDialog({ kind: "edit" })}>
                  <PencilIcon />
                  {t("pc.common.edit")}
                </Button>
                {upstream.status !== "archived" ? (
                  <Button variant="outline" onClick={() => setDialog({ kind: "status" })}>
                    <PowerIcon />
                    {upstream.status === "active" ? t("pc.upstreams.disable") : t("pc.upstreams.enable")}
                  </Button>
                ) : null}
              </>
            }
          />
          <section aria-labelledby="credentials-title" className="flex flex-col gap-3">
            <div className="flex flex-wrap items-end justify-between gap-3">
              <div className="flex min-w-[240px] flex-1 flex-col gap-1">
                <h2 id="credentials-title" className="flex items-center gap-2 text-base font-semibold">
                  <KeyRoundIcon className="size-4 text-muted-foreground" aria-hidden />
                  {t("pc.credentials.title")}
                </h2>
                <p className="text-sm text-muted-foreground">{t("pc.credentials.description")}</p>
              </div>
              <Button onClick={() => setDialog({ kind: "credential" })} disabled={upstream.status === "archived"}>
                <PlusIcon />
                {t("pc.credentials.create")}
              </Button>
            </div>
            {rotation.inProgress && usageKnown ? <RotationBanner remaining={rotation.remaining} /> : null}
            <DataTable
              columns={credentialColumns}
              data={credentials.items}
              loading={credentials.isPending}
              error={credentials.error}
              onRetry={() => void credentials.refetch()}
              getRowId={(row) => row.id}
              minWidth={760}
              rowClassName={(row) => (row.status === "revoked" ? "text-muted-foreground" : undefined)}
              empty={
                <EmptyState
                  icon={<KeyRoundIcon className="size-5" />}
                  title={t("pc.credentials.emptyTitle")}
                  description={t("pc.credentials.emptyBody")}
                />
              }
              footer={
                credentials.hasNextPage ? (
                  <LoadMoreFooter
                    count={credentials.items.length}
                    hasNext
                    loading={credentials.isFetchingNextPage}
                    onLoadMore={() => void credentials.fetchNextPage()}
                  />
                ) : null
              }
            />
          </section>
          <section aria-labelledby="deployments-title" className="flex flex-col gap-3">
            <div className="flex flex-wrap items-end justify-between gap-3">
              <h2 id="deployments-title" className="flex items-center gap-2 text-base font-semibold">
                <GitBranchIcon className="size-4 text-muted-foreground" aria-hidden />
                {t("pc.upstreams.deployments.title")}
              </h2>
              <Button
                variant="outline"
                onClick={() => setDialog({ kind: "deployment", deployment: null })}
                disabled={upstream.status === "archived"}
              >
                <PlusIcon />
                {t("pc.upstreams.deployments.create")}
              </Button>
            </div>
            <DataTable
              columns={deploymentColumns}
              data={deployments.items}
              loading={deployments.isPending}
              error={deployments.error}
              onRetry={() => void deployments.refetch()}
              getRowId={(row) => row.id}
              minWidth={900}
              rowClassName={(row) => (row.status === "archived" ? "text-muted-foreground" : undefined)}
              empty={<EmptyState icon={<GitBranchIcon className="size-5" />} title={t("pc.upstreams.deployments.empty")} />}
            />
          </section>
        </>
      ) : detail.isPending ? (
        <div className="flex justify-center py-12">
          <Spinner />
        </div>
      ) : null}
      {upstream && dialog?.kind === "edit" ? <UpstreamDialog upstream={upstream} onClose={close} /> : null}
      {upstream && dialog?.kind === "status" ? (
        <StatusDialog
          upstream={upstream}
          etag={etagOf(detail.data, upstream.version)}
          onReload={() => void detail.refetch()}
          onClose={close}
        />
      ) : null}
      {upstream && dialog?.kind === "credential" ? <CreateCredentialDialog upstream={upstream} onClose={close} /> : null}
      {dialog?.kind === "revoke" ? (
        <RevokeCredentialDialog
          key={dialog.credential.id}
          upstreamId={upstreamId}
          credential={dialog.credential}
          deployments={usage.get(dialog.credential.id) ?? []}
          onClose={close}
        />
      ) : null}
      {dialog?.kind === "deployment" ? (
        <DeploymentDialog
          key={dialog.deployment?.id ?? "new"}
          deployment={dialog.deployment}
          filters={{ upstreamId }}
          onClose={close}
        />
      ) : null}
    </PageContainer>
  );
}
