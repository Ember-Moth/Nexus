"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import type { ColumnDef } from "@tanstack/react-table";
import { LandmarkIcon, PencilIcon, PlusIcon } from "lucide-react";
import { useMemo, useState } from "react";
import { Controller, useForm, useWatch } from "react-hook-form";
import { toast } from "sonner";
import { z } from "zod";

import { CopyButton } from "@/components/copy-button";
import { DataTable } from "@/components/data-table";
import { ConflictAlert, EmptyState, ErrorAlert, Spinner } from "@/components/feedback";
import { LoadMoreFooter, PageContainer, PageHeader } from "@/components/page";
import { Field, ReadOnlyValue } from "@/components/platform-finance/field";
import { currencyString, nonblank } from "@/components/platform-finance/validation";
import { StatusBadge, toneFor } from "@/components/status-badge";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group";
import { Label } from "@/components/ui/label";
import { useI18n } from "@/i18n/provider";
import { call, nexus } from "@/lib/api/client";
import { isConflict } from "@/lib/api/errors";
import { useCursorList, useIdempotentMutation } from "@/lib/api/hooks";
import type { components } from "@/lib/api/schema";
import { formatDateTime } from "@/lib/format";

type ReceivingAccount = components["schemas"]["ReceivingView"];
type ReceivingCreate = components["schemas"]["ReceivingCreate"];
type ReceivingPatch = components["schemas"]["ReceivingPatch"];

const LIST_KEY = ["receiving-accounts"] as const;

const createSchema = z.object({
  name: nonblank(200),
  reference: z
    .string()
    .trim()
    .min(1, "required")
    .regex(/^[A-Za-z0-9_.:-]{1,70}$/, "reference"),
  currency: currencyString(),
});
type CreateValues = z.input<typeof createSchema>;

function CreateDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  const { t } = useI18n();
  const queryClient = useQueryClient();
  const form = useForm<CreateValues>({
    resolver: zodResolver(createSchema),
    defaultValues: { name: "", reference: "", currency: "USD" },
  });
  const reference = useWatch({ control: form.control, name: "reference" });

  // POST /receiving-accounts requires Idempotency-Key; a lost response is retried with the
  // same key so it cannot create a second account for the same reference.
  const create = useIdempotentMutation<ReceivingAccount, ReceivingCreate>({
    keyPrefix: "receiving-account",
    mutationFn: async (body, idempotencyKey) =>
      (
        await call(() =>
          nexus.POST("/api/v1/receiving-accounts", {
            params: { header: { "Idempotency-Key": idempotencyKey } },
            body,
          }),
        )
      ).data,
    onSuccess: (account) => {
      toast.success(t("pf.receiving.created", { name: account.name }));
      void queryClient.invalidateQueries({ queryKey: LIST_KEY });
      close();
    },
  });

  function close() {
    onOpenChange(false);
    form.reset();
    create.resetOperation();
  }

  const errors = form.formState.errors;
  return (
    <Dialog open={open} onOpenChange={(next) => !next && close()}>
      <DialogContent className="sm:max-w-[480px]">
        <form
          noValidate
          className="grid gap-4"
          onSubmit={form.handleSubmit((values) =>
            create.mutate({
              name: values.name.trim(),
              reference: values.reference.trim(),
              currency: values.currency.trim(),
            }),
          )}
        >
          <DialogHeader>
            <DialogTitle>{t("pf.receiving.createTitle")}</DialogTitle>
            <DialogDescription>{t("pf.receiving.createDescription")}</DialogDescription>
          </DialogHeader>
          {create.error ? <ErrorAlert error={create.error} /> : null}
          <Field id="ra-name" label={t("pf.receiving.name")} error={errors.name?.message}>
            <Input id="ra-name" maxLength={200} autoFocus {...form.register("name")} aria-invalid={Boolean(errors.name) || undefined} />
          </Field>
          <Field
            id="ra-reference"
            label={t("pf.receiving.reference")}
            error={errors.reference?.message}
            hint={t("pf.receiving.referenceHint")}
          >
            <Input
              id="ra-reference"
              maxLength={70}
              autoComplete="off"
              className="font-mono"
              {...form.register("reference")}
              aria-invalid={Boolean(errors.reference) || undefined}
            />
          </Field>
          <div className="grid grid-cols-[120px_1fr] gap-3">
            <Field id="ra-currency" label={t("pf.common.currency")} error={errors.currency?.message}>
              <Input
                id="ra-currency"
                maxLength={3}
                className="font-mono uppercase"
                {...form.register("currency", { setValueAs: (value: string) => value.toUpperCase() })}
                aria-invalid={Boolean(errors.currency) || undefined}
              />
            </Field>
            <ReadOnlyValue label={t("pf.receiving.namespace")} mono>
              manual:{reference.trim() || "…"}
            </ReadOnlyValue>
          </div>
          <p className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs leading-relaxed text-amber-900">
            {t("pf.receiving.immutableNote")}
          </p>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={close}>
              {t("common.cancel")}
            </Button>
            <Button type="submit" disabled={create.isPending}>
              {create.isPending ? <Spinner /> : null}
              {t("common.create")}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

const editSchema = z.object({ name: nonblank(200), status: z.enum(["active", "disabled"]) });
type EditValues = z.input<typeof editSchema>;

function EditDialog({ account, onClose }: { account: ReceivingAccount; onClose: () => void }) {
  const { t } = useI18n();
  const queryClient = useQueryClient();
  const form = useForm<EditValues>({
    resolver: zodResolver(editSchema),
    defaultValues: { name: account.name, status: account.status === "disabled" ? "disabled" : "active" },
  });

  // There is no single GET for receiving accounts; the list row's version is the ETag.
  const save = useMutation({
    mutationFn: async (body: ReceivingPatch) =>
      (
        await call(() =>
          nexus.PATCH("/api/v1/receiving-accounts/{receiving_account_id}", {
            params: {
              path: { receiving_account_id: account.id },
              header: { "If-Match": `"${account.version}"` },
            },
            body,
          }),
        )
      ).data,
    onSuccess: (updated) => {
      toast.success(t("pf.receiving.saved", { name: updated.name }));
      void queryClient.invalidateQueries({ queryKey: LIST_KEY });
      onClose();
    },
  });

  const errors = form.formState.errors;
  const conflict = isConflict(save.error);
  return (
    <Dialog open onOpenChange={(next) => !next && onClose()}>
      <DialogContent className="sm:max-w-[480px]">
        <form
          noValidate
          className="grid gap-4"
          onSubmit={form.handleSubmit((values) => {
            const body: ReceivingPatch = {};
            if (values.name.trim() !== account.name) body.name = values.name.trim();
            if (values.status !== account.status) body.status = values.status;
            if (!body.name && !body.status) return onClose();
            save.mutate(body);
          })}
        >
          <DialogHeader>
            <DialogTitle>{t("pf.receiving.editTitle")}</DialogTitle>
            <DialogDescription>{t("pf.receiving.editDescription")}</DialogDescription>
          </DialogHeader>
          {conflict ? (
            <ConflictAlert
              onReload={() => {
                // The list re-read carries the new version; reopen the row to edit again.
                void queryClient.invalidateQueries({ queryKey: LIST_KEY });
                onClose();
              }}
            />
          ) : save.error ? (
            <ErrorAlert error={save.error} />
          ) : null}
          <div className="grid grid-cols-2 gap-3">
            <ReadOnlyValue label={t("pf.receiving.reference")} mono>
              {account.reference}
            </ReadOnlyValue>
            <ReadOnlyValue label={t("pf.common.currency")} mono>
              {account.currency}
            </ReadOnlyValue>
            <div className="col-span-2">
              <ReadOnlyValue label={t("pf.receiving.namespace")} mono>
                {account.receipt_namespace}
              </ReadOnlyValue>
            </div>
          </div>
          <Field id="ra-edit-name" label={t("pf.receiving.name")} error={errors.name?.message}>
            <Input id="ra-edit-name" maxLength={200} {...form.register("name")} aria-invalid={Boolean(errors.name) || undefined} />
          </Field>
          <fieldset className="grid gap-2">
            <legend className="mb-1.5 text-[13px] font-medium">{t("pf.common.status")}</legend>
            <Controller
              control={form.control}
              name="status"
              render={({ field }) => (
                <RadioGroup value={field.value} onValueChange={field.onChange} className="grid gap-2">
                  {(["active", "disabled"] as const).map((value) => (
                    <Label key={value} className="items-start rounded-md border px-3 py-2 font-normal">
                      <RadioGroupItem value={value} className="mt-0.5" />
                      <span className="flex flex-col gap-0.5">
                        <span className="font-medium">{t(`pf.common.states.${value}`)}</span>
                        <span className="text-xs text-muted-foreground">{t(`pf.receiving.statusHelp.${value}`)}</span>
                      </span>
                    </Label>
                  ))}
                </RadioGroup>
              )}
            />
          </fieldset>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={onClose}>
              {t("common.cancel")}
            </Button>
            <Button type="submit" disabled={save.isPending || conflict}>
              {save.isPending ? <Spinner /> : null}
              {t("common.save")}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

export default function ReceivingAccountsPage() {
  const { t, maybeT } = useI18n();
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<ReceivingAccount | null>(null);

  const accounts = useCursorList<ReceivingAccount>({
    queryKey: [...LIST_KEY, "list"],
    fetchPage: async (cursor) =>
      (await call(() => nexus.GET("/api/v1/receiving-accounts", { params: { query: { limit: 50, cursor } } }))).data,
  });

  const columns = useMemo<ColumnDef<ReceivingAccount, unknown>[]>(
    () => [
      {
        header: t("pf.receiving.name"),
        accessorKey: "name",
        cell: ({ row }) => <span className="font-medium">{row.original.name}</span>,
      },
      {
        header: t("pf.receiving.reference"),
        accessorKey: "reference",
        cell: ({ row }) => (
          <span className="inline-flex items-center gap-1">
            <code className="font-mono text-[12.5px]">{row.original.reference}</code>
            <CopyButton value={row.original.reference} />
          </span>
        ),
      },
      { header: t("pf.common.currency"), accessorKey: "currency", cell: ({ row }) => <code className="font-mono text-xs">{row.original.currency}</code> },
      {
        header: t("pf.receiving.namespace"),
        accessorKey: "receipt_namespace",
        cell: ({ row }) => <code className="font-mono text-xs text-muted-foreground">{row.original.receipt_namespace}</code>,
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
        header: t("pf.common.createdAt"),
        accessorKey: "created_at",
        cell: ({ row }) => <span className="text-muted-foreground tabular-nums">{formatDateTime(row.original.created_at, { seconds: false })}</span>,
      },
      {
        id: "actions",
        header: () => <span className="sr-only">{t("pf.common.actions")}</span>,
        meta: { align: "right" },
        cell: ({ row }) => (
          <Button variant="ghost" size="sm" onClick={() => setEditing(row.original)}>
            <PencilIcon />
            {t("pf.common.edit")}
          </Button>
        ),
      },
    ],
    [t, maybeT],
  );

  return (
    <PageContainer>
      <PageHeader
        title={t("pf.receiving.title")}
        description={t("pf.receiving.subtitle")}
        actions={
          <Button onClick={() => setCreating(true)}>
            <PlusIcon />
            {t("pf.receiving.create")}
          </Button>
        }
      />
      <DataTable
        columns={columns}
        data={accounts.items}
        loading={accounts.isPending}
        error={accounts.error}
        onRetry={() => void accounts.refetch()}
        getRowId={(row) => row.id}
        minWidth={900}
        rowClassName={(row) => (row.status === "disabled" ? "text-muted-foreground" : undefined)}
        empty={
          <EmptyState
            icon={<LandmarkIcon className="size-5" />}
            title={t("pf.receiving.empty")}
            description={t("pf.receiving.emptyBody")}
          />
        }
        footer={
          accounts.items.length > 0 ? (
            <LoadMoreFooter
              count={accounts.items.length}
              hasNext={Boolean(accounts.hasNextPage)}
              loading={accounts.isFetchingNextPage}
              onLoadMore={() => void accounts.fetchNextPage()}
            />
          ) : null
        }
      />
      <CreateDialog open={creating} onOpenChange={setCreating} />
      {editing ? <EditDialog key={editing.id} account={editing} onClose={() => setEditing(null)} /> : null}
    </PageContainer>
  );
}
