"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { MailPlusIcon, MoreHorizontalIcon, ShieldCheckIcon, UsersIcon } from "lucide-react";
import { useMemo, useState } from "react";
import { Controller, useForm } from "react-hook-form";
import { toast } from "sonner";
import { z } from "zod";

import { DataTable } from "@/components/data-table";
import { ConflictAlert, EmptyState, ErrorAlert, Spinner } from "@/components/feedback";
import { LoadMoreFooter, PageContainer, PageHeader } from "@/components/page";
import { hasAnyRole, useOrgWorkspace } from "@/components/shell/workspace";
import { StatusBadge, toneFor } from "@/components/status-badge";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import type { MemberListItem } from "@/components/workspace/lookups";
import { useI18n } from "@/i18n/provider";
import { call, nexus } from "@/lib/api/client";
import { isConflict } from "@/lib/api/errors";
import { useCursorList } from "@/lib/api/hooks";
import { orgKey } from "@/lib/api/queries";
import type { components } from "@/lib/api/schema";
import type { OrgRole } from "@/lib/api/types";
import { formatDateTime } from "@/lib/format";

import { InvitationSecretPanel } from "./invitation-secret";

type Invitation = components["schemas"]["InvitationView"];
type InvitationIssued = components["schemas"]["InvitationIssued"];
const ROLES: OrgRole[] = ["org_owner", "org_developer", "org_finance"];

/** Reads the membership with its ETag; writes send it back as If-Match. */
function useMemberDetail(orgId: string, memberId: string) {
  return useQuery({
    queryKey: orgKey(orgId, "members", "detail", memberId),
    queryFn: async () =>
      call(() =>
        nexus.GET("/api/v1/organizations/{organization_id}/members/{member_id}", {
          params: { path: { organization_id: orgId, member_id: memberId } },
        }),
      ),
    staleTime: 0,
  });
}

function RoleDialog({ orgId, member, onClose }: { orgId: string; member: MemberListItem; onClose: () => void }) {
  const { t } = useI18n();
  const queryClient = useQueryClient();
  const detail = useMemberDetail(orgId, member.id);
  const current = detail.data?.data ?? member;
  const etag = detail.data?.etag ?? `"${current.version}"`;
  const [role, setRole] = useState<OrgRole>(member.role);

  const save = useMutation({
    mutationFn: async (next: OrgRole) =>
      call(() =>
        nexus.PATCH("/api/v1/organizations/{organization_id}/members/{member_id}", {
          params: { path: { organization_id: orgId, member_id: member.id }, header: { "If-Match": etag } },
          body: { role: next },
        }),
      ),
    onSuccess: () => {
      toast.success(t("ws.members.roleSaved", { login: member.login }));
      void queryClient.invalidateQueries({ queryKey: orgKey(orgId, "members") });
      onClose();
    },
  });
  const conflict = isConflict(save.error);

  return (
    <Dialog open onOpenChange={(next) => !next && onClose()}>
      <DialogContent className="sm:max-w-[420px]">
        <DialogHeader>
          <DialogTitle>{t("ws.members.roleTitle", { login: member.login })}</DialogTitle>
          <DialogDescription>{t("ws.members.roleDescription")}</DialogDescription>
        </DialogHeader>
        {conflict ? (
          <ConflictAlert
            onReload={() => {
              save.reset();
              void detail.refetch();
              void queryClient.invalidateQueries({ queryKey: orgKey(orgId, "members") });
            }}
          />
        ) : save.error ? (
          <ErrorAlert error={save.error} />
        ) : null}
        {detail.error ? <ErrorAlert error={detail.error} onRetry={() => void detail.refetch()} /> : null}
        <div className="grid gap-2">
          <Label htmlFor="member-role">{t("ws.members.role")}</Label>
          <Select value={role} onValueChange={(value) => setRole(value as OrgRole)}>
            <SelectTrigger id="member-role" className="w-full">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {ROLES.map((value) => (
                <SelectItem key={value} value={value}>
                  {t(`roles.${value}`)}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          <span className="text-xs text-muted-foreground">{t(`ws.members.roleHints.${role}`)}</span>
        </div>
        <DialogFooter>
          <Button type="button" variant="outline" onClick={onClose}>
            {t("common.cancel")}
          </Button>
          <Button
            type="button"
            disabled={save.isPending || conflict || detail.isFetching || role === current.role}
            onClick={() => save.mutate(role)}
          >
            {save.isPending ? <Spinner /> : null}
            {t("common.save")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function StatusDialog({
  orgId,
  member,
  action,
  onClose,
}: {
  orgId: string;
  member: MemberListItem;
  action: "disable" | "restore";
  onClose: () => void;
}) {
  const { t } = useI18n();
  const queryClient = useQueryClient();
  const detail = useMemberDetail(orgId, member.id);
  const etag = detail.data?.etag ?? `"${(detail.data?.data ?? member).version}"`;

  // DELETE keeps the membership as disabled; restoring is a PATCH back to active.
  const apply = useMutation({
    mutationFn: async () =>
      action === "disable"
        ? call(() =>
            nexus.DELETE("/api/v1/organizations/{organization_id}/members/{member_id}", {
              params: { path: { organization_id: orgId, member_id: member.id }, header: { "If-Match": etag } },
            }),
          )
        : call(() =>
            nexus.PATCH("/api/v1/organizations/{organization_id}/members/{member_id}", {
              params: { path: { organization_id: orgId, member_id: member.id }, header: { "If-Match": etag } },
              body: { status: "active" },
            }),
          ),
    onSuccess: () => {
      toast.success(
        action === "disable"
          ? t("ws.members.disabledToast", { login: member.login })
          : t("ws.members.restoredToast", { login: member.login }),
      );
      void queryClient.invalidateQueries({ queryKey: orgKey(orgId, "members") });
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
            {action === "disable"
              ? t("ws.members.disableTitle", { login: member.login })
              : t("ws.members.restoreTitle", { login: member.login })}
          </DialogTitle>
          <DialogDescription>
            {action === "disable" ? t("ws.members.disableDescription") : t("ws.members.restoreDescription")}
          </DialogDescription>
        </DialogHeader>
        {conflict ? (
          <ConflictAlert
            onReload={() => {
              apply.reset();
              void detail.refetch();
              void queryClient.invalidateQueries({ queryKey: orgKey(orgId, "members") });
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
            className={action === "disable" ? "bg-destructive text-white hover:bg-destructive/90" : undefined}
            disabled={apply.isPending || conflict || detail.isFetching}
            onClick={() => apply.mutate()}
          >
            {apply.isPending ? <Spinner /> : null}
            {action === "disable" ? t("ws.members.disable") : t("ws.members.restore")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

type MemberAction = { member: MemberListItem; kind: "role" | "disable" | "restore" };

function MembersTab({ orgId, myUserId }: { orgId: string; myUserId: string }) {
  const { t, maybeT } = useI18n();
  const [action, setAction] = useState<MemberAction | null>(null);

  const members = useCursorList<MemberListItem>({
    queryKey: orgKey(orgId, "members", "list"),
    fetchPage: async (cursor) =>
      (
        await call(() =>
          nexus.GET("/api/v1/organizations/{organization_id}/members", {
            params: { path: { organization_id: orgId }, query: { limit: 50, cursor } },
          }),
        )
      ).data,
  });

  const columns = useMemo<ColumnDef<MemberListItem, unknown>[]>(
    () => [
      {
        id: "login",
        header: t("ws.members.columns.login"),
        cell: ({ row }) => (
          <span className="flex items-center gap-2">
            <span className="font-medium">{row.original.login}</span>
            {row.original.user_id === myUserId ? <Badge variant="outline">{t("ws.members.you")}</Badge> : null}
          </span>
        ),
      },
      {
        id: "role",
        header: t("ws.members.columns.role"),
        cell: ({ row }) => maybeT(`roles.${row.original.role}`) ?? row.original.role,
      },
      {
        id: "status",
        header: t("ws.members.columns.status"),
        cell: ({ row }) => (
          <StatusBadge tone={toneFor(row.original.status)}>
            {maybeT(`status.${row.original.status}`) ?? row.original.status}
          </StatusBadge>
        ),
      },
      {
        id: "created",
        header: t("ws.members.columns.joined"),
        cell: ({ row }) => (
          <span className="text-muted-foreground tabular-nums">{formatDateTime(row.original.created_at, { seconds: false })}</span>
        ),
      },
      {
        id: "actions",
        header: () => <span className="sr-only">{t("ws.members.columns.actions")}</span>,
        meta: { align: "right" },
        cell: ({ row }) => {
          const member = row.original;
          const active = member.status === "active";
          return (
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button variant="ghost" size="icon-sm" aria-label={t("ws.members.menuLabel", { login: member.login })}>
                  <MoreHorizontalIcon />
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end">
                <DropdownMenuItem disabled={!active} onSelect={() => setAction({ member, kind: "role" })}>
                  {t("ws.members.changeRole")}
                </DropdownMenuItem>
                {active ? (
                  <DropdownMenuItem variant="destructive" onSelect={() => setAction({ member, kind: "disable" })}>
                    {t("ws.members.disable")}…
                  </DropdownMenuItem>
                ) : (
                  <DropdownMenuItem onSelect={() => setAction({ member, kind: "restore" })}>
                    {t("ws.members.restore")}…
                  </DropdownMenuItem>
                )}
              </DropdownMenuContent>
            </DropdownMenu>
          );
        },
      },
    ],
    [t, maybeT, myUserId],
  );

  return (
    <div className="flex flex-col gap-4">
      <p className="text-sm text-muted-foreground">{t("ws.members.membersHint")}</p>
      <DataTable
        columns={columns}
        data={members.items}
        loading={members.isPending}
        error={members.error}
        onRetry={() => void members.refetch()}
        getRowId={(row) => row.id}
        minWidth={720}
        rowClassName={(row) => (row.status !== "active" ? "text-muted-foreground" : undefined)}
        empty={<EmptyState icon={<UsersIcon className="size-5" />} title={t("ws.members.emptyTitle")} />}
        footer={
          members.items.length > 0 ? (
            <LoadMoreFooter
              count={members.items.length}
              hasNext={Boolean(members.hasNextPage)}
              loading={members.isFetchingNextPage}
              onLoadMore={() => void members.fetchNextPage()}
            />
          ) : null
        }
      />
      {action?.kind === "role" ? (
        <RoleDialog key={action.member.id} orgId={orgId} member={action.member} onClose={() => setAction(null)} />
      ) : action ? (
        <StatusDialog
          key={`${action.kind}:${action.member.id}`}
          orgId={orgId}
          member={action.member}
          action={action.kind === "disable" ? "disable" : "restore"}
          onClose={() => setAction(null)}
        />
      ) : null}
    </div>
  );
}

const inviteSchema = z.object({
  login: z
    .string()
    .trim()
    .min(3)
    .max(254)
    .regex(/^\S+$/),
  role: z.enum(["org_owner", "org_developer", "org_finance"]),
});
type InviteValues = z.infer<typeof inviteSchema>;

function InviteDialog({ orgId, open, onClose }: { orgId: string; open: boolean; onClose: () => void }) {
  const { t } = useI18n();
  const queryClient = useQueryClient();
  const [issued, setIssued] = useState<InvitationIssued | null>(null);
  const form = useForm<InviteValues>({
    resolver: zodResolver(inviteSchema),
    defaultValues: { login: "", role: "org_developer" },
  });

  const invite = useMutation({
    mutationFn: async (values: InviteValues) =>
      (
        await call(() =>
          nexus.POST("/api/v1/organizations/{organization_id}/invitations", {
            params: { path: { organization_id: orgId } },
            body: { login: values.login.trim(), role: values.role },
          }),
        )
      ).data,
    onSuccess: (result) => {
      setIssued(result);
      void queryClient.invalidateQueries({ queryKey: orgKey(orgId, "invitations") });
    },
  });

  const close = () => {
    setIssued(null);
    invite.reset();
    form.reset();
    onClose();
  };
  const showingToken = issued !== null;

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        // The token view can only be left with the explicit "Done" button.
        if (!next && !showingToken) close();
      }}
    >
      <DialogContent
        className="sm:max-w-[500px]"
        showCloseButton={!showingToken}
        onEscapeKeyDown={(event) => showingToken && event.preventDefault()}
        onInteractOutside={(event) => showingToken && event.preventDefault()}
      >
        {issued ? (
          <InvitationSecretPanel issued={issued} onDone={close} />
        ) : (
          <form noValidate className="grid gap-5" onSubmit={form.handleSubmit((values) => invite.mutate(values))}>
            <DialogHeader>
              <DialogTitle>{t("ws.invitations.createTitle")}</DialogTitle>
              <DialogDescription>{t("ws.invitations.createDescription")}</DialogDescription>
            </DialogHeader>
            {invite.error ? <ErrorAlert error={invite.error} /> : null}
            <div className="grid gap-2">
              <Label htmlFor="invite-login">{t("ws.invitations.login")}</Label>
              <Input
                id="invite-login"
                autoComplete="off"
                autoFocus
                maxLength={254}
                aria-invalid={Boolean(form.formState.errors.login) || undefined}
                {...form.register("login")}
              />
              <span className={form.formState.errors.login ? "text-xs text-destructive" : "text-xs text-muted-foreground"}>
                {form.formState.errors.login ? t("ws.invitations.loginInvalid") : t("ws.invitations.loginHint")}
              </span>
            </div>
            <div className="grid gap-2">
              <Label htmlFor="invite-role">{t("ws.members.role")}</Label>
              <Controller
                control={form.control}
                name="role"
                render={({ field }) => (
                  <>
                    <Select value={field.value} onValueChange={field.onChange}>
                      <SelectTrigger id="invite-role" className="w-full">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        {ROLES.map((value) => (
                          <SelectItem key={value} value={value}>
                            {t(`roles.${value}`)}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                    <span className="text-xs text-muted-foreground">{t(`ws.members.roleHints.${field.value}`)}</span>
                  </>
                )}
              />
            </div>
            <DialogFooter>
              <Button type="button" variant="outline" onClick={close}>
                {t("common.cancel")}
              </Button>
              <Button type="submit" disabled={invite.isPending}>
                {invite.isPending ? <Spinner /> : null}
                {t("ws.invitations.submit")}
              </Button>
            </DialogFooter>
          </form>
        )}
      </DialogContent>
    </Dialog>
  );
}

function RevokeInvitationDialog({
  orgId,
  invitation,
  onClose,
}: {
  orgId: string;
  invitation: Invitation;
  onClose: () => void;
}) {
  const { t } = useI18n();
  const queryClient = useQueryClient();
  const revoke = useMutation({
    mutationFn: async () =>
      call(() =>
        nexus.DELETE("/api/v1/organizations/{organization_id}/invitations/{invitation_id}", {
          params: { path: { organization_id: orgId, invitation_id: invitation.id } },
        }),
      ),
    onSuccess: () => {
      toast.success(t("ws.invitations.revokedToast", { login: invitation.login }));
      void queryClient.invalidateQueries({ queryKey: orgKey(orgId, "invitations") });
      onClose();
    },
  });
  return (
    <Dialog open onOpenChange={(next) => !next && onClose()}>
      <DialogContent role="alertdialog" className="sm:max-w-[420px]">
        <DialogHeader>
          <DialogTitle>{t("ws.invitations.revokeTitle", { login: invitation.login })}</DialogTitle>
          <DialogDescription>{t("ws.invitations.revokeDescription")}</DialogDescription>
        </DialogHeader>
        {revoke.error ? <ErrorAlert error={revoke.error} /> : null}
        <DialogFooter>
          <Button type="button" variant="outline" onClick={onClose}>
            {t("common.cancel")}
          </Button>
          <Button
            type="button"
            className="bg-destructive text-white hover:bg-destructive/90"
            disabled={revoke.isPending}
            onClick={() => revoke.mutate()}
          >
            {revoke.isPending ? <Spinner /> : null}
            {t("ws.invitations.revoke")}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function InvitationsTab({ orgId }: { orgId: string }) {
  const { t, maybeT } = useI18n();
  const [creating, setCreating] = useState(false);
  const [revoking, setRevoking] = useState<Invitation | null>(null);
  const [now] = useState(() => Date.now());

  const invitations = useCursorList<Invitation>({
    queryKey: orgKey(orgId, "invitations", "list"),
    fetchPage: async (cursor) =>
      (
        await call(() =>
          nexus.GET("/api/v1/organizations/{organization_id}/invitations", {
            params: { path: { organization_id: orgId }, query: { limit: 50, cursor } },
          }),
        )
      ).data,
  });

  const columns = useMemo<ColumnDef<Invitation, unknown>[]>(
    () => [
      {
        id: "login",
        header: t("ws.invitations.columns.login"),
        cell: ({ row }) => <span className="font-medium">{row.original.login}</span>,
      },
      {
        id: "role",
        header: t("ws.invitations.columns.role"),
        cell: ({ row }) => maybeT(`roles.${row.original.role}`) ?? row.original.role,
      },
      {
        id: "created",
        header: t("ws.invitations.columns.created"),
        cell: ({ row }) => (
          <span className="text-muted-foreground tabular-nums">{formatDateTime(row.original.created_at, { seconds: false })}</span>
        ),
      },
      {
        id: "expires",
        header: t("ws.invitations.columns.expires"),
        cell: ({ row }) =>
          Date.parse(row.original.expires_at) <= now ? (
            <StatusBadge tone="neutral">{t("status.expired")}</StatusBadge>
          ) : (
            <span className="tabular-nums">{formatDateTime(row.original.expires_at, { seconds: false })}</span>
          ),
      },
      {
        id: "actions",
        header: () => <span className="sr-only">{t("ws.invitations.columns.actions")}</span>,
        meta: { align: "right" },
        cell: ({ row }) => (
          <Button variant="ghost" size="sm" onClick={() => setRevoking(row.original)}>
            {t("ws.invitations.revoke")}
          </Button>
        ),
      },
    ],
    [t, maybeT, now],
  );

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center gap-3">
        <p className="min-w-[240px] flex-1 text-sm text-muted-foreground">{t("ws.invitations.hint")}</p>
        <Button onClick={() => setCreating(true)}>
          <MailPlusIcon />
          {t("ws.invitations.create")}
        </Button>
      </div>
      <DataTable
        columns={columns}
        data={invitations.items}
        loading={invitations.isPending}
        error={invitations.error}
        onRetry={() => void invitations.refetch()}
        getRowId={(row) => row.id}
        minWidth={680}
        empty={<EmptyState icon={<MailPlusIcon className="size-5" />} title={t("ws.invitations.emptyTitle")} description={t("ws.invitations.emptyBody")} />}
        footer={
          invitations.items.length > 0 ? (
            <LoadMoreFooter
              count={invitations.items.length}
              hasNext={Boolean(invitations.hasNextPage)}
              loading={invitations.isFetchingNextPage}
              onLoadMore={() => void invitations.fetchNextPage()}
            />
          ) : null
        }
      />
      <InviteDialog orgId={orgId} open={creating} onClose={() => setCreating(false)} />
      {revoking ? (
        <RevokeInvitationDialog key={revoking.id} orgId={orgId} invitation={revoking} onClose={() => setRevoking(null)} />
      ) : null}
    </div>
  );
}

export default function MembersPage() {
  const { t } = useI18n();
  const workspace = useOrgWorkspace();
  const { orgId } = workspace;
  const canManage = hasAnyRole(workspace, "org_owner", "platform_admin");

  return (
    <PageContainer>
      <PageHeader title={t("ws.members.title")} description={t("ws.members.subtitle")} />
      {canManage ? (
        <Tabs defaultValue="members">
          <TabsList variant="line">
            <TabsTrigger value="members">{t("ws.members.tabs.members")}</TabsTrigger>
            <TabsTrigger value="invitations">{t("ws.members.tabs.invitations")}</TabsTrigger>
          </TabsList>
          <TabsContent value="members" className="pt-4">
            <MembersTab orgId={orgId} myUserId={workspace.me.user.id} />
          </TabsContent>
          <TabsContent value="invitations" className="pt-4">
            <InvitationsTab orgId={orgId} />
          </TabsContent>
        </Tabs>
      ) : (
        <EmptyState
          icon={<ShieldCheckIcon className="size-5" />}
          title={t("ws.members.noAccessTitle")}
          description={t("ws.members.noAccessBody")}
          className="rounded-xl border"
        />
      )}
    </PageContainer>
  );
}
