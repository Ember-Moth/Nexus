"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { PencilIcon, PercentIcon, PlusIcon, SendIcon } from "lucide-react";
import { Suspense, useState } from "react";
import { useForm, useWatch } from "react-hook-form";
import { toast } from "sonner";
import { z } from "zod";

import { ConflictAlert, EmptyState, Spinner } from "@/components/feedback";
import { OrgPicker, useOrgParam } from "@/components/org-picker";
import { LoadMoreFooter, PageContainer, PageHeader } from "@/components/page";
import { PcErrorAlert } from "@/components/platform-catalog/errors";
import { Field } from "@/components/platform-catalog/form";
import { factorPercent, isFactor, trimDecimal } from "@/components/platform-catalog/pricing";
import { PublishDialog } from "@/components/platform-catalog/publish-dialog";
import type { DiscountVersion } from "@/components/platform-catalog/types";
import { latestStart, useNow, VersionTimeline } from "@/components/platform-catalog/version-timeline";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { useT } from "@/i18n/provider";
import { call, nexus } from "@/lib/api/client";
import { isConflict } from "@/lib/api/errors";
import { useCursorList, useIdempotentMutation } from "@/lib/api/hooks";

const discountsKey = (orgId: string) => ["discounts", orgId] as const;

const schema = z.object({ factor: z.string().trim().refine(isFactor) });
type Values = z.infer<typeof schema>;

/**
 * Create (POST + Idempotency-Key) or edit (PATCH + If-Match) a discount draft. There is no single
 * GET for discount versions, so If-Match is the listed version ("<version>").
 */
function DiscountDialog({
  orgId,
  draft,
  onClose,
}: {
  orgId: string;
  draft: DiscountVersion | null;
  onClose: () => void;
}) {
  const t = useT();
  const queryClient = useQueryClient();
  const form = useForm<Values>({
    resolver: zodResolver(schema),
    defaultValues: { factor: draft ? trimDecimal(draft.factor) : "" },
  });
  const factor = useWatch({ control: form.control, name: "factor" });
  const percent = isFactor(factor ?? "") ? factorPercent(factor) : null;
  const refresh = () => void queryClient.invalidateQueries({ queryKey: discountsKey(orgId) });

  const create = useIdempotentMutation<DiscountVersion, { factor: string }>({
    keyPrefix: "create-discount",
    mutationFn: async (body, idempotencyKey) =>
      (
        await call(() =>
          nexus.POST("/api/v1/organizations/{organization_id}/discount-versions", {
            params: { path: { organization_id: orgId }, header: { "Idempotency-Key": idempotencyKey } },
            body,
          }),
        )
      ).data,
    onSuccess: () => {
      toast.success(t("pc.discounts.form.created"));
      refresh();
      onClose();
    },
  });

  const edit = useMutation({
    mutationFn: async (body: { factor: string }) =>
      (
        await call(() =>
          nexus.PATCH("/api/v1/organizations/{organization_id}/discount-versions/{discount_version_id}", {
            params: {
              path: { organization_id: orgId, discount_version_id: draft!.id },
              header: { "If-Match": `"${draft!.version}"` },
            },
            body,
          }),
        )
      ).data,
    onSuccess: () => {
      toast.success(t("pc.discounts.form.saved"));
      refresh();
      onClose();
    },
  });

  const mutation = draft ? edit : create;
  const conflict = isConflict(mutation.error);

  return (
    <Dialog open onOpenChange={(next) => !next && onClose()}>
      <DialogContent className="sm:max-w-[440px]">
        <form
          noValidate
          className="grid gap-5"
          onSubmit={form.handleSubmit((values) => {
            const body = { factor: values.factor.trim() };
            if (draft) edit.mutate(body);
            else create.mutate(body);
          })}
        >
          <DialogHeader>
            <DialogTitle>{draft ? t("pc.discounts.form.editTitle") : t("pc.discounts.form.createTitle")}</DialogTitle>
            <DialogDescription>{t("pc.discounts.form.description")}</DialogDescription>
          </DialogHeader>
          {conflict ? (
            <ConflictAlert
              onReload={() => {
                refresh();
                onClose();
              }}
            />
          ) : mutation.error ? (
            <PcErrorAlert error={mutation.error} />
          ) : null}
          <Field
            id="discount-factor"
            label={t("pc.discounts.form.factor")}
            hint={percent ? t("pc.discounts.percent", { percent }) : undefined}
            error={form.formState.errors.factor ? t("pc.discounts.form.factorInvalid") : undefined}
          >
            <Input
              id="discount-factor"
              inputMode="decimal"
              autoComplete="off"
              autoFocus
              placeholder="0.85"
              className="font-mono tabular-nums"
              aria-invalid={Boolean(form.formState.errors.factor) || undefined}
              {...form.register("factor")}
            />
          </Field>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={onClose}>
              {t("common.cancel")}
            </Button>
            <Button type="submit" disabled={mutation.isPending || conflict}>
              {mutation.isPending ? <Spinner /> : null}
              {draft ? t("common.save") : t("pc.discounts.create")}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function PublishDiscountDialog({
  orgId,
  version,
  notBefore,
  now,
  onClose,
}: {
  orgId: string;
  version: DiscountVersion;
  notBefore: string | null;
  now: number;
  onClose: () => void;
}) {
  const t = useT();
  const queryClient = useQueryClient();
  const refresh = () => void queryClient.invalidateQueries({ queryKey: discountsKey(orgId) });
  const publish = useIdempotentMutation<DiscountVersion, { ifMatch: string; effective_at: string | null }>({
    keyPrefix: "publish-discount",
    mutationFn: async ({ ifMatch, effective_at }, idempotencyKey) =>
      (
        await call(() =>
          nexus.POST("/api/v1/organizations/{organization_id}/discount-versions/{discount_version_id}/publish", {
            params: {
              path: { organization_id: orgId, discount_version_id: version.id },
              header: { "If-Match": ifMatch, "Idempotency-Key": idempotencyKey },
            },
            body: effective_at ? { effective_at } : {},
          }),
        )
      ).data,
    onSuccess: () => {
      toast.success(t("pc.publish.done"));
      refresh();
      onClose();
    },
  });
  const percent = factorPercent(version.factor);

  return (
    <PublishDialog
      notBefore={notBefore}
      now={now}
      onClose={() => {
        publish.resetOperation();
        onClose();
      }}
      pending={publish.isPending}
      error={publish.error}
      summary={
        <div className="flex items-baseline gap-3 rounded-md border px-3 py-2.5">
          <span className="text-xs text-muted-foreground">{t("pc.discounts.factor")}</span>
          <span className="font-mono text-lg font-semibold tabular-nums">{trimDecimal(version.factor)}</span>
          {percent ? <span className="text-sm text-muted-foreground">{t("pc.discounts.percent", { percent })}</span> : null}
        </div>
      }
      onSubmit={(effectiveAt) => publish.mutate({ ifMatch: `"${version.version}"`, effective_at: effectiveAt })}
      onReload={() => {
        publish.resetOperation();
        refresh();
        onClose();
      }}
    />
  );
}

function DiscountsView() {
  const t = useT();
  const now = useNow();
  const [orgId, setOrgId] = useOrgParam();
  const [editing, setEditing] = useState<DiscountVersion | "new" | null>(null);
  const [publishing, setPublishing] = useState<DiscountVersion | null>(null);

  const discounts = useCursorList<DiscountVersion>({
    queryKey: discountsKey(orgId ?? ""),
    enabled: Boolean(orgId),
    fetchPage: async (cursor) =>
      (
        await call(() =>
          nexus.GET("/api/v1/organizations/{organization_id}/discount-versions", {
            params: { path: { organization_id: orgId! }, query: { limit: 50, cursor } },
          }),
        )
      ).data,
  });

  return (
    <PageContainer>
      <PageHeader
        title={t("pc.discounts.title")}
        description={t("pc.discounts.subtitle")}
        actions={
          <Button disabled={!orgId} onClick={() => setEditing("new")}>
            <PlusIcon />
            {t("pc.discounts.create")}
          </Button>
        }
      />
      <OrgPicker value={orgId} onChange={setOrgId} className="w-fit min-w-64" />
      <p className="text-sm text-muted-foreground">{t("pc.discounts.defaultNote")}</p>
      {!orgId ? (
        <div className="rounded-xl border bg-card">
          <EmptyState icon={<PercentIcon className="size-5" />} title={t("pc.discounts.pickPrompt")} />
        </div>
      ) : discounts.error && discounts.items.length === 0 ? (
        <PcErrorAlert error={discounts.error} onRetry={() => void discounts.refetch()} />
      ) : discounts.isPending ? (
        <div className="flex justify-center py-10">
          <Spinner />
        </div>
      ) : discounts.items.length === 0 ? (
        <div className="rounded-xl border bg-card">
          <EmptyState
            icon={<PercentIcon className="size-5" />}
            title={t("pc.timeline.noVersions")}
            description={t("pc.timeline.noVersionsBody")}
          />
        </div>
      ) : (
        <>
          <section aria-labelledby="discount-timeline" className="overflow-hidden rounded-xl border">
            <div className="flex items-baseline justify-between gap-2 border-b px-4 py-3">
              <h2 id="discount-timeline" className="text-[15px] font-semibold">
                {t("pc.timeline.title")}
              </h2>
              <span className="text-xs text-muted-foreground">{t("pc.timeline.utcNote")}</span>
            </div>
          <VersionTimeline
            className="p-2"
            items={discounts.items}
            now={now}
            title={(item) => {
              const percent = factorPercent(item.factor);
              return (
                <>
                  <span className="font-mono text-base font-semibold tabular-nums">{trimDecimal(item.factor)}</span>
                  {percent ? (
                    <span className="text-sm text-muted-foreground">{t("pc.discounts.percent", { percent })}</span>
                  ) : null}
                </>
              );
            }}
            actions={(item, info) =>
              info.phase === "draft" ? (
                <>
                  <Button variant="outline" size="sm" onClick={() => setEditing(item)}>
                    <PencilIcon />
                    {t("pc.discounts.edit")}
                  </Button>
                  <Button size="sm" onClick={() => setPublishing(item)}>
                    <SendIcon />
                    {t("pc.discounts.publish")}
                  </Button>
                </>
              ) : null
            }
          />
          </section>
          {discounts.hasNextPage || discounts.error ? (
            <div className="overflow-hidden rounded-xl border">
              {discounts.error ? <PcErrorAlert error={discounts.error} className="m-3 w-auto" /> : null}
              <LoadMoreFooter
                count={discounts.items.length}
                hasNext={Boolean(discounts.hasNextPage)}
                loading={discounts.isFetchingNextPage}
                onLoadMore={() => void discounts.fetchNextPage()}
              />
            </div>
          ) : null}
        </>
      )}
      {editing !== null && orgId ? (
        <DiscountDialog
          key={editing === "new" ? "new" : `${editing.id}:${editing.version}`}
          orgId={orgId}
          draft={editing === "new" ? null : editing}
          onClose={() => setEditing(null)}
        />
      ) : null}
      {publishing && orgId ? (
        <PublishDiscountDialog
          key={publishing.id}
          orgId={orgId}
          version={publishing}
          notBefore={latestStart(discounts.items)}
          now={now}
          onClose={() => setPublishing(null)}
        />
      ) : null}
    </PageContainer>
  );
}

export default function DiscountsPage() {
  return (
    <Suspense>
      <DiscountsView />
    </Suspense>
  );
}
