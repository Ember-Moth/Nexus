"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { useQueryClient } from "@tanstack/react-query";
import { EyeIcon, EyeOffIcon } from "lucide-react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { useState } from "react";
import { useForm } from "react-hook-form";
import { z } from "zod";

import { useAuthOptions } from "@/components/auth-layout";
import { Spinner, TraceId } from "@/components/feedback";
import { Turnstile, useTurnstile } from "@/components/turnstile";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { useI18n } from "@/i18n/provider";
import type { Locale } from "@/i18n/types";
import { setCsrfToken } from "@/lib/api/client";
import { ApiError } from "@/lib/api/errors";

const schema = z.object({
  login: z.string().trim().min(1),
  password: z.string().min(1),
});
type Values = z.infer<typeof schema>;

/** Only same-site relative paths are honoured as post-login destinations. */
function safeNext(value: string | null): string {
  return value && value.startsWith("/") && !value.startsWith("//") ? value : "/";
}

export function LoginForm() {
  const { t, maybeT, locale, setLocale } = useI18n();
  const router = useRouter();
  const params = useSearchParams();
  const queryClient = useQueryClient();
  const [showPassword, setShowPassword] = useState(false);
  const [failure, setFailure] = useState<ApiError | null>(null);
  const form = useForm<Values>({ resolver: zodResolver(schema), defaultValues: { login: "", password: "" } });
  const expired = params.get("expired") === "1";
  const options = useAuthOptions();
  const turnstile = useTurnstile();
  const needsTurnstile = options.data?.turnstile ?? false;

  const onSubmit = form.handleSubmit(async (values) => {
    setFailure(null);
    if (needsTurnstile && !turnstile.token) {
      form.setError("root", { message: "turnstile" });
      return;
    }
    let response: Response;
    try {
      response = await fetch("/api/session", {
        method: "POST",
        credentials: "same-origin",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ ...values, turnstile_token: turnstile.token ?? undefined }),
      });
    } catch (error) {
      setFailure(ApiError.network(error));
      return;
    } finally {
      turnstile.reset();
    }
    const body = await response.json().catch(() => null);
    if (response.status !== 201) {
      setFailure(ApiError.fromResponse(response, body));
      form.setValue("password", "");
      return;
    }
    setCsrfToken((body as { csrf_token: string }).csrf_token);
    queryClient.clear();
    router.replace(safeNext(params.get("next")));
  });

  const submitting = form.formState.isSubmitting;
  const invalid = Boolean(form.formState.errors.login || form.formState.errors.password);

  let failureText: string | null = null;
  if (failure) {
    if (failure.status === 429) {
      failureText = failure.retryAfter
        ? t("login.rateLimited", { seconds: failure.retryAfter })
        : t("login.rateLimitedNoWait");
    } else if (failure.status === 401 || failure.status === 422) {
      failureText = t("login.invalid");
    } else if (failure.status === 0) {
      failureText = t("errors.network");
    } else {
      failureText = maybeT(`errors.codes.${failure.code}`) ?? failure.message;
    }
  }

  return (
    <div className="flex min-h-screen flex-col bg-muted/40">
      <header className="flex h-16 items-center gap-2.5 px-6 md:px-8">
        <span aria-hidden className="flex size-7 items-center justify-center rounded-md bg-primary text-sm font-bold text-primary-foreground">
          N
        </span>
        <span className="flex-1 text-[15px] font-semibold">{t("app.name")}</span>
        <ToggleGroup
          type="single"
          size="sm"
          variant="outline"
          value={locale}
          aria-label={t("login.language")}
          onValueChange={(value) => value && setLocale(value as Locale)}
        >
          <ToggleGroupItem value="zh-CN">中文</ToggleGroupItem>
          <ToggleGroupItem value="en">English</ToggleGroupItem>
        </ToggleGroup>
      </header>
      <div className="flex flex-1 items-center justify-center px-4 pb-24 pt-6">
        <form
          onSubmit={onSubmit}
          noValidate
          className="flex w-full max-w-[380px] flex-col gap-5 rounded-2xl border bg-card p-7 shadow-sm"
        >
          <div className="flex flex-col gap-1.5">
            <h1 className="text-[22px] font-semibold tracking-tight">{t("login.title")}</h1>
            <p className="text-sm text-muted-foreground">{t("login.subtitle")}</p>
          </div>

          {expired && !failure ? (
            <Alert>
              <AlertTitle>{t("errors.sessionExpiredTitle")}</AlertTitle>
              <AlertDescription>{t("errors.sessionExpiredBody")}</AlertDescription>
            </Alert>
          ) : null}

          {failure ? (
            <Alert
              variant={failure.status === 429 ? "default" : "destructive"}
              className={failure.status === 429 ? "border-amber-200 bg-amber-50 text-amber-900" : undefined}
            >
              <AlertTitle>{failureText}</AlertTitle>
              <AlertDescription>
                <TraceId traceId={failure.traceId} />
              </AlertDescription>
            </Alert>
          ) : null}

          <div className="flex flex-col gap-2">
            <Label htmlFor="login">{t("login.login")}</Label>
            <Input
              id="login"
              autoComplete="username"
              autoFocus
              aria-invalid={Boolean(form.formState.errors.login) || undefined}
              {...form.register("login")}
            />
          </div>
          <div className="flex flex-col gap-2">
            <div className="flex items-center justify-between">
              <Label htmlFor="password">{t("login.password")}</Label>
              <Link href="/forgot-password" className="text-[13px] text-muted-foreground underline-offset-3 hover:underline">
                {t("id.login.forgot")}
              </Link>
            </div>
            <div className="relative">
              <Input
                id="password"
                type={showPassword ? "text" : "password"}
                autoComplete="current-password"
                className="pr-10"
                aria-invalid={Boolean(form.formState.errors.password || (failure && failure.status === 401)) || undefined}
                {...form.register("password")}
              />
              <Button
                type="button"
                variant="ghost"
                size="icon-sm"
                className="absolute top-0.5 right-0.5"
                aria-label={showPassword ? t("login.hidePassword") : t("login.showPassword")}
                onClick={() => setShowPassword((value) => !value)}
              >
                {showPassword ? <EyeOffIcon /> : <EyeIcon />}
              </Button>
            </div>
          </div>
          {invalid ? <p className="-mt-2 text-sm text-destructive">{t("login.required")}</p> : null}
          {needsTurnstile && options.data?.turnstile_site_key ? (
            <Turnstile siteKey={options.data.turnstile_site_key} action="login" onToken={turnstile.setToken} widgetRef={turnstile.widget} />
          ) : null}
          {form.formState.errors.root?.message === "turnstile" ? (
            <p className="-mt-2 text-sm text-destructive">{t("id.shared.turnstileRequired")}</p>
          ) : null}
          <Button type="submit" size="lg" disabled={submitting}>
            {submitting ? <Spinner /> : null}
            {submitting ? t("login.submitting") : t("login.submit")}
          </Button>
          <div className="h-px bg-border" />
          <p className="text-[13px] leading-relaxed text-muted-foreground">
            <Link href="/register" className="font-medium text-foreground underline underline-offset-3">
              {t("id.login.register")}
            </Link>
            {" · "}
            {t("login.setupHint")}{" "}
            <Link href="/setup-password" className="font-medium text-foreground underline underline-offset-3">
              {t("login.setupLink")}
            </Link>
          </p>
        </form>
      </div>
      <footer className="px-8 pb-6 text-center text-xs text-muted-foreground">{t("login.footer")}</footer>
    </div>
  );
}
