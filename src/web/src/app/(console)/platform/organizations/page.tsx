"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { Building2Icon, PlusIcon } from "lucide-react";
import Link from "next/link";
import { useMemo, useState } from "react";
import { toast } from "sonner";

import { DataTable } from "@/components/data-table";
import { EmptyState } from "@/components/feedback";
import { NameDialog } from "@/components/name-dialog";
import { LoadMoreFooter, PageContainer, PageHeader } from "@/components/page";
import { hasAnyRole, useWorkspace } from "@/components/shell/workspace";
import { StatusBadge, toneFor } from "@/components/status-badge";
import { Button } from "@/components/ui/button";
import { useI18n } from "@/i18n/provider";
import { call, nexus } from "@/lib/api/client";
import { useCursorList } from "@/lib/api/hooks";
import type { Organization } from "@/lib/api/types";
import { formatDateTime } from "@/lib/format";

export default function OrganizationsPage() {
  const { t, maybeT } = useI18n();
  const workspace = useWorkspace();
  const queryClient = useQueryClient();
  const [createOpen, setCreateOpen] = useState(false);
  const canCreate = hasAnyRole(workspace, "platform_admin");

  const organizations = useCursorList<Organization>({
    queryKey: ["organizations", "platform"],
    fetchPage: async (cursor) =>
      (await call(() => nexus.GET("/api/v1/organizations", { params: { query: { limit: 50, cursor } } }))).data,
    // New organizations stay "provisioning" until the worker opens their account.
    refetchInterval: 5_000,
  });

  const create = useMutation({
    mutationFn: async (name: string) =>
      (await call(() => nexus.POST("/api/v1/organizations", { body: { name } }))).data,
    onSuccess: (organization) => {
      toast.success(t("organizations.createdToast", { name: organization.name }));
      setCreateOpen(false);
      void queryClient.invalidateQueries({ queryKey: ["organizations"] });
      // The creator becomes the first owner when owner_user_id is omitted.
      void queryClient.invalidateQueries({ queryKey: ["me"] });
    },
  });

  const columns = useMemo<ColumnDef<Organization, unknown>[]>(
    () => [
      {
        header: t("organizations.columns.name"),
        accessorKey: "name",
        cell: ({ row }) => <span className="font-medium">{row.original.name}</span>,
      },
      {
        header: t("organizations.columns.status"),
        accessorKey: "status",
        cell: ({ row }) => (
          <StatusBadge tone={toneFor(row.original.status)}>{maybeT(`status.${row.original.status}`) ?? row.original.status}</StatusBadge>
        ),
      },
      {
        header: t("organizations.columns.created"),
        accessorKey: "created_at",
        cell: ({ row }) => <span className="text-muted-foreground tabular-nums">{formatDateTime(row.original.created_at, { seconds: false })}</span>,
      },
      {
        id: "actions",
        header: () => <span className="sr-only">{t("organizations.columns.actions")}</span>,
        meta: { align: "right" },
        cell: ({ row }) => (
          <Button variant="outline" size="sm" asChild>
            <Link href={`/o/${row.original.id}/overview`}>{t("organizations.enter")}</Link>
          </Button>
        ),
      },
    ],
    [t, maybeT],
  );

  return (
    <PageContainer>
      <PageHeader
        title={t("organizations.title")}
        description={t("organizations.subtitle")}
        actions={
          canCreate ? (
            <Button onClick={() => setCreateOpen(true)}>
              <PlusIcon />
              {t("organizations.create")}
            </Button>
          ) : null
        }
      />
      <DataTable
        columns={columns}
        data={organizations.items}
        loading={organizations.isPending}
        error={organizations.error}
        onRetry={() => void organizations.refetch()}
        getRowId={(row) => row.id}
        minWidth={640}
        empty={<EmptyState icon={<Building2Icon className="size-5" />} title={t("organizations.emptyTitle")} />}
        footer={
          organizations.items.length > 0 ? (
            <LoadMoreFooter
              count={organizations.items.length}
              hasNext={Boolean(organizations.hasNextPage)}
              loading={organizations.isFetchingNextPage}
              onLoadMore={() => void organizations.fetchNextPage()}
            />
          ) : null
        }
      />
      <NameDialog
        open={createOpen}
        onOpenChange={(open) => {
          setCreateOpen(open);
          if (!open) create.reset();
        }}
        title={t("organizations.create")}
        description={t("organizations.ownerHint")}
        label={t("organizations.name")}
        pending={create.isPending}
        error={create.error}
        onSubmit={(name) => create.mutate(name)}
      />
    </PageContainer>
  );
}
