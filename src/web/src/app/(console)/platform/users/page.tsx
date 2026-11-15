"use client";

import type { ColumnDef } from "@tanstack/react-table";
import { KeyRoundIcon, PlusIcon, ShieldIcon, UsersIcon } from "lucide-react";
import { useMemo, useState } from "react";

import { DataTable } from "@/components/data-table";
import { EmptyState } from "@/components/feedback";
import { LoadMoreFooter, PageContainer, PageHeader } from "@/components/page";
import type { UserView } from "@/components/platform-catalog/types";
import { useWorkspace } from "@/components/shell/workspace";
import { StatusBadge, toneFor } from "@/components/status-badge";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { useI18n } from "@/i18n/provider";
import { call, nexus } from "@/lib/api/client";
import { useCursorList } from "@/lib/api/hooks";
import { formatDateTime } from "@/lib/format";

import { AuthSettingsCard } from "./auth-settings";
import { CreateUserDialog, ResetTokenDialog, RolesDialog, usersKey } from "./user-dialogs";

type Dialog = { kind: "create" } | { kind: "roles"; user: UserView } | { kind: "reset"; user: UserView };

export default function UsersPage() {
  const { t, maybeT } = useI18n();
  const workspace = useWorkspace();
  const selfId = workspace.me.user.id;
  const [dialog, setDialog] = useState<Dialog | null>(null);

  const users = useCursorList<UserView>({
    queryKey: [...usersKey, "list"],
    fetchPage: async (cursor) =>
      (await call(() => nexus.GET("/api/v1/users", { params: { query: { limit: 50, cursor } } }))).data,
  });

  const columns = useMemo<ColumnDef<UserView, unknown>[]>(
    () => [
      {
        header: t("pc.users.columns.login"),
        accessorKey: "login",
        cell: ({ row }) => (
          <span className="flex items-center gap-2">
            <span className="font-mono text-[12.5px]">{row.original.login}</span>
            {row.original.id === selfId ? <Badge variant="outline">{t("pc.users.you")}</Badge> : null}
          </span>
        ),
      },
      {
        header: t("pc.users.columns.status"),
        accessorKey: "status",
        cell: ({ row }) => (
          <StatusBadge tone={toneFor(row.original.status)}>
            {maybeT(`pc.users.status.${row.original.status}`) ?? maybeT(`status.${row.original.status}`) ?? row.original.status}
          </StatusBadge>
        ),
      },
      {
        header: t("pc.users.columns.created"),
        accessorKey: "created_at",
        cell: ({ row }) => (
          <span className="text-muted-foreground tabular-nums">{formatDateTime(row.original.created_at, { seconds: false })}</span>
        ),
      },
      {
        id: "actions",
        header: () => <span className="sr-only">{t("pc.users.columns.actions")}</span>,
        meta: { align: "right" },
        cell: ({ row }) => (
          <span className="inline-flex gap-2">
            <Button variant="outline" size="sm" onClick={() => setDialog({ kind: "roles", user: row.original })}>
              <ShieldIcon />
              {t("pc.users.roles")}
            </Button>
            <Button variant="outline" size="sm" onClick={() => setDialog({ kind: "reset", user: row.original })}>
              <KeyRoundIcon />
              {t("pc.users.resetToken")}
            </Button>
          </span>
        ),
      },
    ],
    [t, maybeT, selfId],
  );

  const close = () => setDialog(null);

  return (
    <PageContainer>
      <PageHeader
        title={t("pc.users.title")}
        description={t("pc.users.subtitle")}
        actions={
          <Button onClick={() => setDialog({ kind: "create" })}>
            <PlusIcon />
            {t("pc.users.create")}
          </Button>
        }
      />
      <DataTable
        columns={columns}
        data={users.items}
        loading={users.isPending}
        error={users.error}
        onRetry={() => void users.refetch()}
        getRowId={(row) => row.id}
        minWidth={720}
        empty={<EmptyState icon={<UsersIcon className="size-5" />} title={t("pc.users.emptyTitle")} />}
        footer={
          users.items.length > 0 ? (
            <LoadMoreFooter
              count={users.items.length}
              hasNext={Boolean(users.hasNextPage)}
              loading={users.isFetchingNextPage}
              onLoadMore={() => void users.fetchNextPage()}
            />
          ) : null
        }
      />
      <AuthSettingsCard />
      {dialog?.kind === "create" ? <CreateUserDialog onClose={close} /> : null}
      {dialog?.kind === "roles" ? (
        <RolesDialog key={dialog.user.id} user={dialog.user} isSelf={dialog.user.id === selfId} onClose={close} />
      ) : null}
      {dialog?.kind === "reset" ? <ResetTokenDialog key={dialog.user.id} user={dialog.user} onClose={close} /> : null}
    </PageContainer>
  );
}
