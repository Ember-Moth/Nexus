"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { UserPlusIcon, UsersIcon } from "lucide-react";
import { useMemo, useState } from "react";
import { Controller, useForm } from "react-hook-form";
import { toast } from "sonner";
import { z } from "zod";

import { DataTable } from "@/components/data-table";
import { ConflictAlert, EmptyState, ErrorAlert, Spinner } from "@/components/feedback";
import { StatusBadge, toneFor } from "@/components/status-badge";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { collectPages, useMemberOptions, type MemberListItem } from "@/components/workspace/lookups";
import { useI18n } from "@/i18n/provider";
import { call, nexus } from "@/lib/api/client";
import { isConflict } from "@/lib/api/errors";
import { orgKey } from "@/lib/api/queries";
import type { components } from "@/lib/api/schema";
import type { Project } from "@/lib/api/types";
import { formatDateTime, shortId } from "@/lib/format";

type Grant = components["schemas"]["GrantView"];

/** All grants of the organization (up to 1000); the list endpoint has no project filter. */
function useGrants(orgId: string, enabled: boolean) {
  return useQuery({
    queryKey: orgKey(orgId, "project-grants", "all"),
    queryFn: async () =>
      collectPages<Grant>(async (cursor) =>
        (
          await call(() =>
            nexus.GET("/api/v1/organizations/{organization_id}/project-grants", {
              params: { path: { organization_id: orgId }, query: { limit: 100, cursor } },
            }),
          )
        ).data,
      ),
    enabled,
  });
}

const grantSchema = z.object({ membershipId: z.string().min(1) });

function GrantDialog({
  orgId,
  project,
  candidates,
  open,
  onClose,
}: {
  orgId: string;
  project: Project;
  candidates: MemberListItem[];
  open: boolean;
  onClose: () => void;
}) {
  const { t } = useI18n();
  const queryClient = useQueryClient();
  const form = useForm<{ membershipId: string }>({
    resolver: zodResolver(grantSchema),
    defaultValues: { membershipId: "" },
  });
  const create = useMutation({
    mutationFn: async (membershipId: string) =>
      (
        await call(() =>
          nexus.POST("/api/v1/organizations/{organization_id}/project-grants", {
            params: { path: { organization_id: orgId } },
            body: { project_id: project.id, membership_id: membershipId },
          }),
        )
      ).data,
    onSuccess: () => {
      toast.success(t("ws.grants.granted"));
      void queryClient.invalidateQueries({ queryKey: orgKey(orgId, "project-grants") });
      close();
    },
  });
  function close() {
    form.reset();
    create.reset();
    onClose();
  }
  return (
    <Dialog open={open} onOpenChange={(next) => !next && close()}>
      <DialogContent className="sm:max-w-[440px]">
        <form noValidate className="grid gap-5" onSubmit={form.handleSubmit((values) => create.mutate(values.membershipId))}>
          <DialogHeader>
            <DialogTitle>{t("ws.grants.createTitle", { project: project.name })}</DialogTitle>
            <DialogDescription>{t("ws.grants.createDescription")}</DialogDescription>
          </DialogHeader>
          {create.error ? <ErrorAlert error={create.error} /> : null}
          <div className="grid gap-2">
            <Label htmlFor="grant-member">{t("ws.grants.member")}</Label>
            <Controller
              control={form.control}
              name="membershipId"
              render={({ field, fieldState }) => (
                <Select value={field.value} onValueChange={field.onChange} disabled={candidates.length === 0}>
                  <SelectTrigger id="grant-member" className="w-full" aria-invalid={fieldState.invalid || undefined}>
                    <SelectValue placeholder={candidates.length ? t("ws.grants.selectMember") : t("ws.grants.noCandidates")} />
                  </SelectTrigger>
                  <SelectContent>
                    {candidates.map((member) => (
                      <SelectItem key={member.id} value={member.id}>
                        {member.login}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              )}
            />
            <span className="text-xs text-muted-foreground">{t("ws.grants.memberHint")}</span>
          </div>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={close}>
              {t("common.cancel")}
            </Button>
            <Button type="submit" disabled={create.isPending || candidates.length === 0}>
              {create.isPending ? <Spinner /> : null}
              {t("ws.grants.submit")}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function GrantStatusDialog({
  orgId,
  grant,
  login,
  onClose,
}: {
  orgId: string;
  grant: Grant;
  login: string;
  onClose: () => void;
}) {
  const { t } = useI18n();
  const queryClient = useQueryClient();
  const revoking = grant.status === "active";
  // The single GET supplies the ETag for If-Match.
  const detail = useQuery({
    queryKey: orgKey(orgId, "project-grants", "detail", grant.id),
    queryFn: async () =>
      call(() =>
        nexus.GET("/api/v1/organizations/{organization_id}/project-grants/{grant_id}", {
          params: { path: { organization_id: orgId, grant_id: grant.id } },
        }),
      ),
    staleTime: 0,
  });
  const etag = detail.data?.etag ?? `"${(detail.data?.data ?? grant).version}"`;

  // DELETE keeps the grant as revoked; restoring is a PATCH back to active.
  const apply = useMutation({
    mutationFn: async () =>
      revoking
        ? call(() =>
            nexus.DELETE("/api/v1/organizations/{organization_id}/project-grants/{grant_id}", {
              params: { path: { organization_id: orgId, grant_id: grant.id }, header: { "If-Match": etag } },
            }),
          )
        : call(() =>
            nexus.PATCH("/api/v1/organizations/{organization_id}/project-grants/{grant_id}", {
              params: { path: { organization_id: orgId, grant_id: grant.id }, header: { "If-Match": etag } },
              body: { status: "active" },
            }),
          ),
    onSuccess: () => {
      toast.success(revoking ? t("ws.grants.revokedToast", { login }) : t("ws.grants.restoredToast", { login }));
      void queryClient.invalidateQueries({ queryKey: orgKey(orgId, "project-grants") });
      onClose();
    },
  });
  const conflict = isConflict(apply.error);

  return (
    <Dialog open onOpenChange={(next) => !next && onClose()}>
      <DialogContent role="alertdialog" className="sm:max-w-[420px]">
        <DialogHeader>
          <DialogTitle>
            {revoking ? t("ws.grants.revokeTitle", { login }) : t("ws.grants.restoreTitle", { login })}
          </DialogTitle>
          <DialogDescription>
            {revoking ? t("ws.grants.revokeDescription") : t("ws.grants.restoreDescription")}
          </DialogDescription>
        </DialogHeader>
        {conflict ? (
          <ConflictAlert
            onReload={() => {
              apply.reset();
              void detail.refetch();
              void queryClient.invalidateQueries({ queryKey: orgKey(orgId, "project-grants") });
            }}
          />
        ) : apply.error ? (
          <ErrorAlert error={apply.error} />
        ) : null}
        {detail.error ? <ErrorAlert error={detail.error} onRetry={() => void detail.refetch()} /> : null}
        <DialogFooter>
          <Button type="button" variant="outline" onClick={onClose}>
            {t("common.cancel")}
          </Button>
          <Button
            type="button"
            className={revoking ? "bg-destructive text-white hover:bg-destructive/90" : undefined}
            disabled={apply.isPending || conflict || detail.isFetching}
            onClick={() => apply.mutate()}
          >
            {apply.isPending ? <Spinner /> : null}
            {revoking ? t("ws.grants.revoke") : t("ws.grants.restore")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/**
 * "开发者授权" tab of a project. Only owners and platform admins can list or change grants;
 * only active org_developer members can be granted (project-credentials.md).
 */
export function ProjectGrants({ orgId, project, canManage }: { orgId: string; project: Project; canManage: boolean }) {
  const { t, maybeT } = useI18n();
  const grants = useGrants(orgId, canManage);
  const members = useMemberOptions(orgId, canManage);
  const [creating, setCreating] = useState(false);
  const [changing, setChanging] = useState<Grant | null>(null);

  const memberById = useMemo(() => new Map((members.data ?? []).map((m) => [m.id, m])), [members.data]);
  const rows = useMemo(
    () => (grants.data?.items ?? []).filter((grant) => grant.project_id === project.id),
    [grants.data, project.id],
  );
  const candidates = useMemo(() => {
    const granted = new Set(rows.map((grant) => grant.membership_id));
    return (members.data ?? []).filter(
      (member) => member.role === "org_developer" && member.status === "active" && !granted.has(member.id),
    );
  }, [members.data, rows]);
  const loginOf = (membershipId: string) => memberById.get(membershipId)?.login ?? shortId(membershipId);

  const columns = useMemo<ColumnDef<Grant, unknown>[]>(
    () => [
      {
        id: "member",
        header: t("ws.grants.columns.member"),
        cell: ({ row }) => {
          const member = memberById.get(row.original.membership_id);
          return (
            <span className="flex flex-col">
              <span className="font-medium" title={row.original.membership_id}>
                {member?.login ?? shortId(row.original.membership_id)}
              </span>
              {member && (member.role !== "org_developer" || member.status !== "active") ? (
                <span className="text-xs text-amber-800">
                  {t("ws.grants.memberChanged", {
                    role: maybeT(`roles.${member.role}`) ?? member.role,
                    status: maybeT(`status.${member.status}`) ?? member.status,
                  })}
                </span>
              ) : null}
            </span>
          );
        },
      },
      {
        id: "status",
        header: t("ws.grants.columns.status"),
        cell: ({ row }) => (
          <StatusBadge tone={toneFor(row.original.status)}>
            {maybeT(`status.${row.original.status}`) ?? row.original.status}
          </StatusBadge>
        ),
      },
      {
        id: "created",
        header: t("ws.grants.columns.created"),
        cell: ({ row }) => (
          <span className="text-muted-foreground tabular-nums">{formatDateTime(row.original.created_at, { seconds: false })}</span>
        ),
      },
      {
        id: "actions",
        header: () => <span className="sr-only">{t("ws.grants.columns.actions")}</span>,
        meta: { align: "right" },
        cell: ({ row }) => (
          <Button variant="ghost" size="sm" onClick={() => setChanging(row.original)}>
            {row.original.status === "active" ? t("ws.grants.revoke") : t("ws.grants.restore")}
          </Button>
        ),
      },
    ],
    [t, maybeT, memberById],
  );

  if (!canManage) {
    return <p className="text-sm text-muted-foreground">{t("ws.grants.ownerOnly")}</p>;
  }

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center gap-3">
        <p className="min-w-[240px] flex-1 text-sm text-muted-foreground">{t("ws.grants.hint")}</p>
        <Button variant="outline" onClick={() => setCreating(true)} disabled={project.status !== "active" || !members.data}>
          <UserPlusIcon />
          {t("ws.grants.create")}
        </Button>
      </div>
      {members.error ? <ErrorAlert error={members.error} onRetry={() => void members.refetch()} /> : null}
      <DataTable
        columns={columns}
        data={rows}
        loading={grants.isPending}
        error={grants.error}
        onRetry={() => void grants.refetch()}
        getRowId={(row) => row.id}
        minWidth={620}
        rowClassName={(row) => (row.status !== "active" ? "text-muted-foreground" : undefined)}
        empty={
          <EmptyState
            icon={<UsersIcon className="size-5" />}
            title={t("ws.grants.emptyTitle")}
            description={t("ws.grants.emptyBody")}
            className="py-8"
          />
        }
      />
      {grants.data?.truncated ? <p className="text-xs text-muted-foreground">{t("ws.grants.truncated")}</p> : null}
      <GrantDialog orgId={orgId} project={project} candidates={candidates} open={creating} onClose={() => setCreating(false)} />
      {changing ? (
        <GrantStatusDialog
          key={changing.id}
          orgId={orgId}
          grant={changing}
          login={loginOf(changing.membership_id)}
          onClose={() => setChanging(null)}
        />
      ) : null}
    </div>
  );
}
