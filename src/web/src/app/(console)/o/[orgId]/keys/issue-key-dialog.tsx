"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Controller, useForm, useWatch } from "react-hook-form";
import { z } from "zod";

import { ErrorAlert, Spinner } from "@/components/feedback";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { useT } from "@/i18n/provider";
import { call, nexus } from "@/lib/api/client";
import { useIdempotentMutation } from "@/lib/api/hooks";
import { orgKey, useProjectOptions, useServiceAccountOptions } from "@/lib/api/queries";
import type { ApiKey, ApiKeyIssued } from "@/lib/api/types";
import { utcInputToIso } from "@/lib/format";

import { hasSecret, ReplayPanel, SecretPanel } from "./secret-panel";

const schema = z
  .object({
    serviceAccountId: z.string().min(1),
    name: z.string().trim().min(1).max(200),
    expiry: z.enum(["none", "custom"]),
    expiryDate: z.string(),
    expiryTime: z.string(),
  })
  .superRefine((value, ctx) => {
    if (value.expiry !== "custom") return;
    const iso = utcInputToIso(value.expiryDate, value.expiryTime);
    if (!iso || Date.parse(iso) <= Date.now()) {
      ctx.addIssue({ code: "custom", path: ["expiryDate"], message: "expiry" });
    }
  });
type Values = z.infer<typeof schema>;

interface IssueVariables {
  service_account_id: string;
  name: string;
  expires_at: string | null;
}

export function IssueKeyDialog({
  orgId,
  open,
  onOpenChange,
  defaultServiceAccountId,
  onRotate,
}: {
  orgId: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  defaultServiceAccountId?: string;
  onRotate: (key: ApiKey) => void;
}) {
  const t = useT();
  const queryClient = useQueryClient();
  const [result, setResult] = useState<ApiKeyIssued | null>(null);
  const accounts = useServiceAccountOptions(orgId, undefined, open);
  const projects = useProjectOptions(orgId, open);
  const projectName = (id: string) => projects.data?.find((p) => p.id === id)?.name ?? "";
  const activeAccounts = (accounts.data ?? []).filter((account) => account.status === "active");

  const form = useForm<Values>({
    resolver: zodResolver(schema),
    defaultValues: {
      serviceAccountId: defaultServiceAccountId ?? "",
      name: "",
      expiry: "none",
      expiryDate: "",
      expiryTime: "00:00",
    },
  });

  // POST /api-keys requires Idempotency-Key. The key is kept while the form values are
  // unchanged, so a retry after a lost response returns the original key (200) instead of
  // creating a second one.
  const issue = useIdempotentMutation<ApiKeyIssued, IssueVariables>({
    keyPrefix: "issue-key",
    mutationFn: async (body, idempotencyKey) =>
      (
        await call(() =>
          nexus.POST("/api/v1/organizations/{organization_id}/api-keys", {
            params: { path: { organization_id: orgId }, header: { "Idempotency-Key": idempotencyKey } },
            body,
          }),
        )
      ).data,
    onSuccess: (issued) => {
      setResult(issued);
      void queryClient.invalidateQueries({ queryKey: orgKey(orgId, "api-keys") });
    },
  });

  const reset = () => {
    setResult(null);
    issue.resetOperation();
    form.reset({ serviceAccountId: defaultServiceAccountId ?? "", name: "", expiry: "none", expiryDate: "", expiryTime: "00:00" });
  };

  const showingSecret = result !== null && hasSecret(result);
  const close = () => {
    onOpenChange(false);
    reset();
  };

  const onSubmit = form.handleSubmit((values) => {
    issue.mutate({
      service_account_id: values.serviceAccountId,
      name: values.name.trim(),
      expires_at: values.expiry === "custom" ? utcInputToIso(values.expiryDate, values.expiryTime) : null,
    });
  });

  const watchedAccount = useWatch({ control: form.control, name: "serviceAccountId" });
  const expiry = useWatch({ control: form.control, name: "expiry" });
  const selectedAccount = activeAccounts.find((a) => a.id === (result?.key.service_account_id ?? watchedAccount));
  const accountLabel = selectedAccount ? `${selectedAccount.name} · ${projectName(selectedAccount.project_id)}` : undefined;

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        // The secret view can only be left with the explicit "Done" button.
        if (!next && showingSecret) return;
        if (!next) close();
        else onOpenChange(true);
      }}
    >
      <DialogContent
        className="sm:max-w-[460px]"
        showCloseButton={!showingSecret}
        onEscapeKeyDown={(event) => showingSecret && event.preventDefault()}
        onInteractOutside={(event) => showingSecret && event.preventDefault()}
      >
        {result && hasSecret(result) ? (
          <SecretPanel issued={result} serviceAccountName={accountLabel} onDone={close} />
        ) : result ? (
          <ReplayPanel
            issued={result}
            onClose={close}
            onRotate={() => {
              const key = result.key;
              close();
              onRotate(key);
            }}
          />
        ) : (
          <form onSubmit={onSubmit} noValidate className="grid gap-5">
            <DialogHeader>
              <DialogTitle>{t("keys.issueDialog.title")}</DialogTitle>
              <DialogDescription>{t("keys.issueDialog.description")}</DialogDescription>
            </DialogHeader>
            {issue.error ? <ErrorAlert error={issue.error} /> : null}
            <div className="grid gap-2">
              <Label htmlFor="issue-sa">{t("keys.issueDialog.serviceAccount")}</Label>
              <Controller
                control={form.control}
                name="serviceAccountId"
                render={({ field, fieldState }) => (
                  <Select value={field.value} onValueChange={field.onChange}>
                    <SelectTrigger id="issue-sa" className="w-full" aria-invalid={fieldState.invalid || undefined}>
                      <SelectValue
                        placeholder={
                          accounts.isPending
                            ? t("common.loading")
                            : activeAccounts.length
                              ? t("keys.issueDialog.selectServiceAccount")
                              : t("keys.issueDialog.noServiceAccounts")
                        }
                      />
                    </SelectTrigger>
                    <SelectContent>
                      {activeAccounts.map((account) => (
                        <SelectItem key={account.id} value={account.id}>
                          {account.name}
                          <span className="text-muted-foreground">· {projectName(account.project_id)}</span>
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                )}
              />
            </div>
            <div className="grid gap-2">
              <Label htmlFor="issue-name">{t("keys.issueDialog.name")}</Label>
              <Input
                id="issue-name"
                maxLength={200}
                aria-invalid={Boolean(form.formState.errors.name) || undefined}
                aria-describedby="issue-name-hint"
                {...form.register("name")}
              />
              <span id="issue-name-hint" className="text-xs text-muted-foreground">
                {t("keys.issueDialog.nameHint")}
              </span>
            </div>
            <fieldset className="grid gap-2">
              <legend className="mb-2 text-sm font-medium">{t("keys.issueDialog.expiry")}</legend>
              <Controller
                control={form.control}
                name="expiry"
                render={({ field }) => (
                  <RadioGroup value={field.value} onValueChange={field.onChange} className="gap-2">
                    <Label className="font-normal">
                      <RadioGroupItem value="none" />
                      {t("keys.issueDialog.noExpiry")}
                    </Label>
                    <Label className="font-normal">
                      <RadioGroupItem value="custom" />
                      {t("keys.issueDialog.customExpiry")}
                    </Label>
                  </RadioGroup>
                )}
              />
              {expiry === "custom" ? (
                <div className="flex flex-col gap-1.5 pl-6">
                  <div className="flex gap-2">
                    <Input
                      type="date"
                      aria-label={t("keys.issueDialog.expiryDate")}
                      aria-invalid={Boolean(form.formState.errors.expiryDate) || undefined}
                      {...form.register("expiryDate")}
                    />
                    <Input type="time" aria-label={t("keys.issueDialog.expiryTime")} className="w-28" {...form.register("expiryTime")} />
                  </div>
                  {form.formState.errors.expiryDate ? (
                    <span className="text-xs text-destructive">{t("keys.issueDialog.expiryInvalid")}</span>
                  ) : null}
                </div>
              ) : null}
            </fieldset>
            <DialogFooter>
              <Button type="button" variant="outline" onClick={close}>
                {t("common.cancel")}
              </Button>
              <Button type="submit" disabled={issue.isPending}>
                {issue.isPending ? <Spinner /> : null}
                {issue.isPending ? t("keys.issueDialog.submitting") : t("keys.issueDialog.submit")}
              </Button>
            </DialogFooter>
          </form>
        )}
      </DialogContent>
    </Dialog>
  );
}
