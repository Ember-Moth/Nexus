"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { FolderIcon, PlusIcon } from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { Suspense, useMemo, useState } from "react";
import { toast } from "sonner";

import { DataTable } from "@/components/data-table";
import { ConflictAlert, EmptyState, ErrorAlert, LoadingRows, useErrorToast } from "@/components/feedback";
import { NameDialog } from "@/components/name-dialog";
import { LoadMoreFooter, PageContainer, PageHeader } from "@/components/page";
import { hasAnyRole, useOrgWorkspace } from "@/components/shell/workspace";
import { StatusBadge, toneFor } from "@/components/status-badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useI18n } from "@/i18n/provider";
import { call, nexus } from "@/lib/api/client";
import { isConflict } from "@/lib/api/errors";
import { useCursorList } from "@/lib/api/hooks";
import { orgKey } from "@/lib/api/queries";
import type { Project, ServiceAccount } from "@/lib/api/types";
import { formatDate, formatDateTime } from "@/lib/format";
import { cn } from "@/lib/utils";
import { ProjectGrants } from "@/components/workspace/project-grants";

import { IssueKeyDialog } from "../keys/issue-key-dialog";
import { RotateKeyDialog } from "../keys/key-actions";
import { ProjectPolicies } from "../policies/policy-editor";
import type { ApiKey } from "@/lib/api/types";

function ServiceAccounts({ orgId, project, canManage }: { orgId: string; project: Project; canManage: boolean }) {
  const { t, maybeT } = useI18n();
  const queryClient = useQueryClient();
  const showError = useErrorToast();
  const [createOpen, setCreateOpen] = useState(false);
  const [issueFor, setIssueFor] = useState<string | null>(null);
  const [rotating, setRotating] = useState<ApiKey | null>(null);
  const listKey = orgKey(orgId, "service-accounts", "project", project.id);

  const accounts = useCursorList<ServiceAccount>({
    queryKey: listKey,
    fetchPage: async (cursor) =>
      (
        await call(() =>
          nexus.GET("/api/v1/organizations/{organization_id}/service-accounts", {
            params: { path: { organization_id: orgId }, query: { project_id: project.id, limit: 50, cursor } },
          }),
        )
      ).data,
  });

  const invalidate = () => queryClient.invalidateQueries({ queryKey: orgKey(orgId, "service-accounts") });

  const create = useMutation({
    mutationFn: async (name: string) =>
      (
        await call(() =>
          nexus.POST("/api/v1/organizations/{organization_id}/service-accounts", {
            params: { path: { organization_id: orgId } },
            body: { name, project_id: project.id },
          }),
        )
      ).data,
    onSuccess: (account) => {
      toast.success(t("projects.createdToast", { name: account.name }));
      setCreateOpen(false);
      void invalidate();
    },
  });

  // Status changes are conditional writes: the row's version is the ETag ("<version>").
  const toggle = useMutation({
    mutationFn: async (account: ServiceAccount) =>
      call(() =>
        nexus.PATCH("/api/v1/organizations/{organization_id}/service-accounts/{account_id}", {
          params: {
            path: { organization_id: orgId, account_id: account.id },
            header: { "If-Match": `"${account.version}"` },
          },
          body: { status: account.status === "active" ? "disabled" : "active" },
        }),
      ),
    onSuccess: () => void invalidate(),
    onError: (error) => {
      if (isConflict(error)) void invalidate();
      showError(error);
    },
  });

  const columns = useMemo<ColumnDef<ServiceAccount, unknown>[]>(
    () => [
      {
        header: t("projects.columns.name"),
        accessorKey: "name",
        cell: ({ row }) => <span className="font-mono text-[12.5px] font-medium">{row.original.name}</span>,
      },
      {
        header: t("projects.columns.status"),
        accessorKey: "status",
        cell: ({ row }) => (
          <StatusBadge tone={toneFor(row.original.status)}>
            {maybeT(`status.${row.original.status}`) ?? row.original.status}
          </StatusBadge>
        ),
      },
      {
        header: t("projects.columns.created"),
        accessorKey: "created_at",
        cell: ({ row }) => <span className="text-muted-foreground tabular-nums">{formatDateTime(row.original.created_at, { seconds: false })}</span>,
      },
      {
        id: "actions",
        header: () => <span className="sr-only">{t("projects.columns.actions")}</span>,
        meta: { align: "right" },
        cell: ({ row }) =>
          canManage ? (
            <span className="flex justify-end gap-1.5">
              <Button
                variant="outline"
                size="sm"
                disabled={row.original.status !== "active"}
                onClick={() => setIssueFor(row.original.id)}
              >
                {t("projects.issueKey")}
              </Button>
              <Button
                variant="ghost"
                size="sm"
                disabled={toggle.isPending}
                onClick={() => toggle.mutate(row.original)}
              >
                {row.original.status === "active" ? t("projects.disable") : t("projects.enable")}
              </Button>
            </span>
          ) : null,
      },
    ],
    [t, maybeT, canManage, toggle],
  );

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center gap-3">
        <p className="min-w-[240px] flex-1 text-sm text-muted-foreground">{t("projects.serviceAccountsHint")}</p>
        {canManage ? (
          <Button variant="outline" onClick={() => setCreateOpen(true)} disabled={project.status !== "active"}>
            <PlusIcon />
            {t("projects.createServiceAccount")}
          </Button>
        ) : null}
      </div>
      <DataTable
        columns={columns}
        data={accounts.items}
        loading={accounts.isPending}
        error={accounts.error}
        onRetry={() => void accounts.refetch()}
        getRowId={(row) => row.id}
        minWidth={620}
        empty={<EmptyState title={t("projects.saEmpty")} className="py-8" />}
        footer={
          accounts.hasNextPage ? (
            <LoadMoreFooter
              count={accounts.items.length}
              hasNext
              loading={accounts.isFetchingNextPage}
              onLoadMore={() => void accounts.fetchNextPage()}
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
        title={t("projects.createServiceAccount")}
        label={t("projects.serviceAccountName")}
        pending={create.isPending}
        error={create.error}
        onSubmit={(name) => create.mutate(name)}
      />
      <IssueKeyDialog
        key={issueFor ?? "none"}
        orgId={orgId}
        open={issueFor !== null}
        onOpenChange={(open) => !open && setIssueFor(null)}
        defaultServiceAccountId={issueFor ?? undefined}
        onRotate={setRotating}
      />
      <RotateKeyDialog orgId={orgId} apiKey={rotating} onOpenChange={(open) => !open && setRotating(null)} />
    </div>
  );
}

function ProjectDetail({ orgId, projectId, canManageProject, canManageAccounts }: {
  orgId: string;
  projectId: string;
  canManageProject: boolean;
  canManageAccounts: boolean;
}) {
  const { t, maybeT } = useI18n();
  const queryClient = useQueryClient();
  const detail = useQuery({
    queryKey: orgKey(orgId, "projects", "detail", projectId),
    queryFn: async () =>
      call(() =>
        nexus.GET("/api/v1/organizations/{organization_id}/projects/{project_id}", {
          params: { path: { organization_id: orgId, project_id: projectId } },
        }),
      ),
  });
  const toggle = useMutation({
    mutationFn: async ({ etag, status }: { etag: string; status: "active" | "disabled" }) =>
      call(() =>
        nexus.PATCH("/api/v1/organizations/{organization_id}/projects/{project_id}", {
          params: { path: { organization_id: orgId, project_id: projectId }, header: { "If-Match": etag } },
          body: { status },
        }),
      ),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: orgKey(orgId, "projects") }),
  });

  if (detail.error) return <ErrorAlert error={detail.error} onRetry={() => void detail.refetch()} />;
  if (!detail.data) return <LoadingRows rows={3} />;
  const project = detail.data.data;
  const etag = detail.data.etag ?? `"${project.version}"`;

  return (
    <div className="flex flex-col gap-5">
      <div className="flex flex-wrap items-start gap-3">
        <div className="flex min-w-[240px] flex-1 flex-col gap-1.5">
          <div className="flex items-center gap-2.5">
            <h2 className="text-2xl font-semibold tracking-tight">{project.name}</h2>
            <StatusBadge tone={toneFor(project.status)}>{maybeT(`status.${project.status}`) ?? project.status}</StatusBadge>
          </div>
          <p className="text-sm text-muted-foreground">
            {t("projects.created", { date: formatDate(project.created_at), version: project.version })}
          </p>
        </div>
        {canManageProject && project.status !== "archived" ? (
          <Button
            variant="outline"
            disabled={toggle.isPending}
            onClick={() => toggle.mutate({ etag, status: project.status === "active" ? "disabled" : "active" })}
          >
            {project.status === "active" ? t("projects.disable") : t("projects.enable")}
          </Button>
        ) : null}
      </div>
      {isConflict(toggle.error) ? (
        <ConflictAlert
          onReload={() => {
            toggle.reset();
            void queryClient.invalidateQueries({ queryKey: orgKey(orgId, "projects") });
          }}
        />
      ) : toggle.error ? (
        <ErrorAlert error={toggle.error} />
      ) : null}
      <Tabs defaultValue="service-accounts">
        <TabsList variant="line">
          <TabsTrigger value="service-accounts">{t("projects.tabs.serviceAccounts")}</TabsTrigger>
          <TabsTrigger value="keys">{t("projects.tabs.keys")}</TabsTrigger>
          <TabsTrigger value="grants">{t("projects.tabs.grants")}</TabsTrigger>
          <TabsTrigger value="policies">{t("projects.tabs.policies")}</TabsTrigger>
        </TabsList>
        <TabsContent value="service-accounts" className="pt-4">
          <ServiceAccounts orgId={orgId} project={project} canManage={canManageAccounts} />
        </TabsContent>
        <TabsContent value="keys" className="pt-4 text-sm">
          <Link className="underline underline-offset-3" href={`/o/${orgId}/keys`}>
            {t("nav.items.keys")} →
          </Link>
        </TabsContent>
        <TabsContent value="grants" className="pt-4">
          <ProjectGrants orgId={orgId} project={project} canManage={canManageProject} />
        </TabsContent>
        <TabsContent value="policies" className="pt-4">
          <ProjectPolicies orgId={orgId} projectId={project.id} />
        </TabsContent>
      </Tabs>
    </div>
  );
}

function ProjectsView() {
  const { t, maybeT } = useI18n();
  const workspace = useOrgWorkspace();
  const { orgId } = workspace;
  const router = useRouter();
  const pathname = usePathname();
  const params = useSearchParams();
  const queryClient = useQueryClient();
  const [filter, setFilter] = useState("");
  const [createOpen, setCreateOpen] = useState(false);
  const canManageProject = hasAnyRole(workspace, "org_owner", "platform_admin");
  const canManageAccounts = hasAnyRole(workspace, "org_owner", "org_developer", "platform_admin");

  const projects = useCursorList<Project>({
    queryKey: orgKey(orgId, "projects", "list"),
    fetchPage: async (cursor) =>
      (
        await call(() =>
          nexus.GET("/api/v1/organizations/{organization_id}/projects", {
            params: { path: { organization_id: orgId }, query: { limit: 50, cursor } },
          }),
        )
      ).data,
  });
  const selectedId = params.get("project") ?? projects.items[0]?.id;
  const select = (id: string) => router.replace(`${pathname}?project=${id}`, { scroll: false });

  const create = useMutation({
    mutationFn: async (name: string) =>
      (
        await call(() =>
          nexus.POST("/api/v1/organizations/{organization_id}/projects", {
            params: { path: { organization_id: orgId } },
            body: { name },
          }),
        )
      ).data,
    onSuccess: (project) => {
      toast.success(t("projects.createdToast", { name: project.name }));
      setCreateOpen(false);
      void queryClient.invalidateQueries({ queryKey: orgKey(orgId, "projects") });
      select(project.id);
    },
  });

  const shown = projects.items.filter((p) => p.name.toLowerCase().includes(filter.trim().toLowerCase()));

  return (
    <PageContainer>
      <PageHeader title={t("projects.title")} description={t("projects.subtitle")} />
      <div className="flex flex-wrap items-start gap-6">
        <section aria-labelledby="project-list" className="w-full max-w-[320px] min-w-0 flex-[1_1_260px] rounded-xl border bg-card">
          <div className="flex items-center gap-2 border-b px-4 py-3">
            <h2 id="project-list" className="flex-1 text-[15px] font-semibold">
              {t("projects.title")}
            </h2>
            {canManageProject ? (
              <Button size="sm" onClick={() => setCreateOpen(true)}>
                {t("projects.create")}
              </Button>
            ) : null}
          </div>
          <div className="p-3">
            <Input
              aria-label={t("projects.filter")}
              placeholder={t("projects.filter")}
              value={filter}
              onChange={(event) => setFilter(event.target.value)}
              className="h-8"
            />
          </div>
          {projects.error ? (
            <div className="p-3">
              <ErrorAlert error={projects.error} onRetry={() => void projects.refetch()} />
            </div>
          ) : projects.isPending ? (
            <LoadingRows rows={3} columns={1} />
          ) : (
            <ul className="flex flex-col gap-0.5 px-2 pb-2">
              {shown.map((project) => (
                <li key={project.id}>
                  <button
                    type="button"
                    aria-current={project.id === selectedId ? "true" : undefined}
                    onClick={() => select(project.id)}
                    className={cn(
                      "flex w-full items-center gap-2 rounded-lg px-2.5 py-2 text-left hover:bg-muted",
                      project.id === selectedId && "bg-muted",
                    )}
                  >
                    <span className="flex-1 truncate font-medium">{project.name}</span>
                    {project.status !== "active" ? (
                      <StatusBadge tone={toneFor(project.status)}>{maybeT(`status.${project.status}`) ?? project.status}</StatusBadge>
                    ) : null}
                  </button>
                </li>
              ))}
              {projects.hasNextPage ? (
                <li>
                  <Button variant="ghost" size="sm" className="w-full" onClick={() => void projects.fetchNextPage()}>
                    {t("common.loadMore")}
                  </Button>
                </li>
              ) : null}
            </ul>
          )}
        </section>
        <div className="min-w-0 flex-[999_1_520px]">
          {selectedId ? (
            <ProjectDetail
              key={selectedId}
              orgId={orgId}
              projectId={selectedId}
              canManageProject={canManageProject}
              canManageAccounts={canManageAccounts}
            />
          ) : projects.isPending ? null : (
            <EmptyState
              icon={<FolderIcon className="size-5" />}
              title={t("projects.emptyTitle")}
              description={t("projects.emptyBody")}
              action={canManageProject ? <Button onClick={() => setCreateOpen(true)}>{t("projects.create")}</Button> : undefined}
              className="rounded-xl border"
            />
          )}
        </div>
      </div>
      <NameDialog
        open={createOpen}
        onOpenChange={(open) => {
          setCreateOpen(open);
          if (!open) create.reset();
        }}
        title={t("projects.createTitle")}
        label={t("projects.name")}
        pending={create.isPending}
        error={create.error}
        onSubmit={(name) => create.mutate(name)}
      />
    </PageContainer>
  );
}

export default function ProjectsPage() {
  return (
    <Suspense>
      <ProjectsView />
    </Suspense>
  );
}
