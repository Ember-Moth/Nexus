"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import Link from "next/link";
import { useState } from "react";
import { useForm } from "react-hook-form";
import { z } from "zod";

import { ErrorAlert, Spinner } from "@/components/feedback";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useT } from "@/i18n/provider";
import { ApiError } from "@/lib/api/errors";

const schema = z.object({ token: z.string().trim().min(1), password: z.string().min(16) });
type Values = z.infer<typeof schema>;

export default function SetupPasswordPage() {
  const t = useT();
  const [error, setError] = useState<ApiError | null>(null);
  const [done, setDone] = useState(false);
  const form = useForm<Values>({ resolver: zodResolver(schema), defaultValues: { token: "", password: "" } });

  const onSubmit = form.handleSubmit(async (values) => {
    setError(null);
    try {
      const response = await fetch("/api/password", {
        method: "POST",
        credentials: "same-origin",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(values),
      });
      if (response.status !== 204) {
        setError(ApiError.fromResponse(response, await response.json().catch(() => null)));
        return;
      }
      setDone(true);
    } catch (cause) {
      setError(ApiError.network(cause));
    }
  });

  return (
    <div className="flex min-h-screen items-center justify-center bg-muted/40 px-4">
      <form onSubmit={onSubmit} noValidate className="flex w-full max-w-[400px] flex-col gap-5 rounded-2xl border bg-card p-7 shadow-sm">
        <div className="flex flex-col gap-1.5">
          <h1 className="text-[22px] font-semibold tracking-tight">{t("setup.title")}</h1>
          <p className="text-sm text-muted-foreground">{t("setup.subtitle")}</p>
        </div>
        {done ? (
          <Alert>
            <AlertDescription>
              {t("setup.success")}{" "}
              <Link href="/login" className="font-medium text-foreground underline">
                {t("login.submit")}
              </Link>
            </AlertDescription>
          </Alert>
        ) : (
          <>
            {error ? <ErrorAlert error={error} /> : null}
            <div className="flex flex-col gap-2">
              <Label htmlFor="token">{t("setup.token")}</Label>
              <Input id="token" autoComplete="one-time-code" aria-invalid={Boolean(form.formState.errors.token) || undefined} {...form.register("token")} />
            </div>
            <div className="flex flex-col gap-2">
              <Label htmlFor="new-password">{t("setup.password")}</Label>
              <Input id="new-password" type="password" autoComplete="new-password" aria-invalid={Boolean(form.formState.errors.password) || undefined} {...form.register("password")} />
            </div>
            <Button type="submit" size="lg" disabled={form.formState.isSubmitting}>
              {form.formState.isSubmitting ? <Spinner /> : null}
              {t("setup.submit")}
            </Button>
          </>
        )}
      </form>
    </div>
  );
}
