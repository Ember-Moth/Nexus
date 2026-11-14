"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { useForm } from "react-hook-form";
import { z } from "zod";

import { ErrorAlert, Spinner } from "@/components/feedback";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useT } from "@/i18n/provider";

const schema = z.object({ name: z.string().trim().min(1).max(200) });

/** Small create dialog for resources that only need a name (projects, service accounts, organizations). */
export function NameDialog({
  open,
  onOpenChange,
  title,
  description,
  label,
  pending,
  error,
  onSubmit,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  description?: string;
  label: string;
  pending: boolean;
  error: unknown;
  onSubmit: (name: string) => void;
}) {
  const t = useT();
  const form = useForm<{ name: string }>({ resolver: zodResolver(schema), defaultValues: { name: "" } });
  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) form.reset();
        onOpenChange(next);
      }}
    >
      <DialogContent className="sm:max-w-[420px]">
        <form className="grid gap-5" noValidate onSubmit={form.handleSubmit((values) => onSubmit(values.name.trim()))}>
          <DialogHeader>
            <DialogTitle>{title}</DialogTitle>
            {description ? <DialogDescription>{description}</DialogDescription> : null}
          </DialogHeader>
          {error ? <ErrorAlert error={error} /> : null}
          <div className="grid gap-2">
            <Label htmlFor="resource-name">{label}</Label>
            <Input
              id="resource-name"
              maxLength={200}
              autoFocus
              aria-invalid={Boolean(form.formState.errors.name) || undefined}
              {...form.register("name")}
            />
          </div>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>
              {t("common.cancel")}
            </Button>
            <Button type="submit" disabled={pending}>
              {pending ? <Spinner /> : null}
              {t("common.create")}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
