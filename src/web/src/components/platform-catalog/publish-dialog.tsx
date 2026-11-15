"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { TriangleAlertIcon } from "lucide-react";
import type { ReactNode } from "react";
import { Controller, useForm, useWatch } from "react-hook-form";
import { z } from "zod";

import { ConflictAlert, Spinner } from "@/components/feedback";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group";
import { useT } from "@/i18n/provider";
import { isConflict } from "@/lib/api/errors";
import { formatDateTime, utcInputToIso } from "@/lib/format";
import { cn } from "@/lib/utils";

import { PcErrorAlert } from "./errors";

type Values = { mode: "now" | "scheduled"; date: string; time: string };

function schemaFor(notBefore: string | null) {
  return z
    .object({ mode: z.enum(["now", "scheduled"]), date: z.string(), time: z.string() })
    .superRefine((value, ctx) => {
      if (value.mode !== "scheduled") return;
      const iso = utcInputToIso(value.date, value.time);
      const floor = Math.max(Date.now(), notBefore ? Date.parse(notBefore) : 0);
      if (!iso || Date.parse(iso) <= floor) ctx.addIssue({ code: "custom", path: ["date"], message: "time" });
    });
}

export interface PublishFormProps {
  /** effective_at of the latest published version in the scope; a new one must start after it. */
  notBefore: string | null;
  /** Server time is unknown here; "now" is only offered when nothing is scheduled. */
  now: number;
  pending: boolean;
  error: unknown;
  /** Reason the submit button is disabled, e.g. unsaved draft edits. */
  blocked?: string;
  onSubmit: (effectiveAt: string | null) => void;
  onReload: () => void;
  onCancel?: () => void;
  className?: string;
}

/**
 * Publishing a price or discount version: `{}` publishes at server time, otherwise a UTC
 * effective_at that is in the future and later than the latest published start. The caller
 * owns the mutation (If-Match + Idempotency-Key).
 */
export function PublishForm({
  notBefore,
  now,
  pending,
  error,
  blocked,
  onSubmit,
  onReload,
  onCancel,
  className,
}: PublishFormProps) {
  const t = useT();
  const scheduledAhead = notBefore !== null && Date.parse(notBefore) > now;
  const form = useForm<Values>({
    resolver: zodResolver(schemaFor(notBefore)),
    defaultValues: { mode: scheduledAhead ? "scheduled" : "now", date: "", time: "00:00" },
  });
  const mode = useWatch({ control: form.control, name: "mode" });
  const conflict = isConflict(error);

  const submit = form.handleSubmit((values) =>
    onSubmit(values.mode === "scheduled" ? utcInputToIso(values.date, values.time) : null),
  );

  return (
    <form noValidate onSubmit={submit} className={cn("grid gap-4", className)}>
      {conflict ? <ConflictAlert onReload={onReload} /> : error ? <PcErrorAlert error={error} /> : null}
      <Controller
        control={form.control}
        name="mode"
        render={({ field }) => (
          <RadioGroup
            value={field.value}
            onValueChange={field.onChange}
            aria-label={t("pc.timeline.effectiveAt")}
            className="grid gap-2.5 sm:grid-cols-2"
          >
            <Label
              className={cn(
                "items-start rounded-md border px-3 py-2.5 font-normal",
                field.value === "now" && "border-foreground bg-muted/40",
                scheduledAhead && "text-muted-foreground",
              )}
            >
              <RadioGroupItem value="now" className="mt-0.5" disabled={scheduledAhead} />
              <span className="flex flex-col gap-0.5">
                <span className="font-medium">{t("pc.publish.now")}</span>
                <span className="text-xs text-muted-foreground">
                  {scheduledAhead
                    ? t("pc.publish.nowBlocked", { time: formatDateTime(notBefore, { seconds: false }) })
                    : t("pc.publish.nowHint")}
                </span>
              </span>
            </Label>
            <Label
              className={cn(
                "items-start rounded-md border px-3 py-2.5 font-normal",
                field.value === "scheduled" && "border-foreground bg-muted/40",
              )}
            >
              <RadioGroupItem value="scheduled" className="mt-0.5" />
              <span className="flex min-w-0 flex-col gap-1.5">
                <span className="font-medium">{t("pc.publish.scheduled")}</span>
                {mode === "scheduled" ? (
                  <span className="flex flex-wrap items-center gap-2">
                    <Input
                      type="date"
                      className="h-8 w-[150px]"
                      aria-label={t("pc.publish.date")}
                      aria-invalid={Boolean(form.formState.errors.date) || undefined}
                      {...form.register("date")}
                    />
                    <Input type="time" className="h-8 w-[104px]" aria-label={t("pc.publish.time")} {...form.register("time")} />
                    <span className="text-xs text-muted-foreground">{t("common.utc")}</span>
                  </span>
                ) : (
                  <span className="text-xs text-muted-foreground">{t("pc.publish.scheduledHint")}</span>
                )}
              </span>
            </Label>
          </RadioGroup>
        )}
      />
      {form.formState.errors.date ? (
        <span role="alert" className="-mt-2 text-xs text-destructive">
          {notBefore
            ? t("pc.publish.invalidTimeAfter", { time: formatDateTime(notBefore, { seconds: false }) })
            : t("pc.publish.invalidTime")}
        </span>
      ) : null}
      <div className="flex gap-2.5 rounded-md border border-amber-200 bg-amber-50 px-3 py-2.5 text-[13px] text-amber-900">
        <TriangleAlertIcon className="mt-0.5 size-4 shrink-0 text-amber-700" aria-hidden />
        <span>{t("pc.publish.warning")}</span>
      </div>
      <div className="flex flex-wrap items-center justify-end gap-2">
        {blocked ? <span className="mr-auto text-xs text-muted-foreground">{blocked}</span> : null}
        {onCancel ? (
          <Button type="button" variant="outline" onClick={onCancel}>
            {t("common.cancel")}
          </Button>
        ) : null}
        <Button type="submit" disabled={pending || conflict || Boolean(blocked)}>
          {pending ? <Spinner /> : null}
          {pending ? t("pc.publish.submitting") : t("pc.publish.submit")}
        </Button>
      </div>
    </form>
  );
}

/** PublishForm in a dialog, for pages that list versions without a detail pane. */
export function PublishDialog({
  onClose,
  summary,
  ...props
}: Omit<PublishFormProps, "onCancel"> & { onClose: () => void; summary?: ReactNode }) {
  const t = useT();
  return (
    <Dialog open onOpenChange={(next) => !next && onClose()}>
      <DialogContent className="sm:max-w-[560px]">
        <DialogHeader>
          <DialogTitle>{t("pc.publish.title")}</DialogTitle>
          <DialogDescription>{t("pc.publish.description")}</DialogDescription>
        </DialogHeader>
        {summary}
        <PublishForm {...props} onCancel={onClose} />
      </DialogContent>
    </Dialog>
  );
}
