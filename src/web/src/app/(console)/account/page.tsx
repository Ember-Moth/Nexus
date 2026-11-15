"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { CheckIcon } from "lucide-react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";
import { useForm } from "react-hook-form";
import { toast } from "sonner";
import { z } from "zod";

import { useAuthOptions } from "@/components/auth-layout";
import { ErrorAlert, Spinner } from "@/components/feedback";
import { AccountFrame } from "@/components/shell/account-frame";
import { StatusBadge } from "@/components/status-badge";
import { Turnstile, useTurnstile } from "@/components/turnstile";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useI18n } from "@/i18n/provider";
import { call, nexus } from "@/lib/api/client";
import { isApiError } from "@/lib/api/errors";
import { useMe } from "@/lib/api/hooks";

const bindSchema = z.object({
  email: z.string().trim().regex(/^[^\s@]+@[^\s@]+\.[^\s@]+$/).max(254),
  password: z.string().min(1).max(1024),
});
const verifySchema = z.object({ token: z.string().trim().min(1).max(128) });

function Account() {
  const { t, maybeT } = useI18n();
  const params = useSearchParams();
  const queryClient = useQueryClient();
  const me = useMe();
  const options = useAuthOptions();
  const bindTurnstile = useTurnstile();
  const verifyTurnstile = useTurnstile();
  const [sentTo, setSentTo] = useState<string | null>(null);
  const needsTurnstile = options.data?.turnstile ?? false;
  const siteKey = options.data?.turnstile_site_key;

  const bindForm = useForm<z.infer<typeof bindSchema>>({ resolver: zodResolver(bindSchema), defaultValues: { email: "", password: "" } });
  const verifyForm = useForm<z.infer<typeof verifySchema>>({
    resolver: zodResolver(verifySchema),
    defaultValues: { token: params.get("token") ?? "" },
  });

  // POST /me/email (202): sends a one-time token to the new address.
  const bind = useMutation({
    mutationFn: async (values: z.infer<typeof bindSchema>) =>
      call(() =>
        nexus.POST("/api/v1/me/email", {
          body: { email: values.email.trim(), password: values.password, turnstile_token: bindTurnstile.token ?? undefined },
        }),
      ),
    onSuccess: (_, values) => {
      setSentTo(values.email.trim());
      bindForm.reset({ email: values.email.trim(), password: "" });
    },
    onSettled: () => bindTurnstile.reset(),
  });
  // POST /me/email/verify (204): binds the address; GET /me then shows verified_email.
  const verify = useMutation({
    mutationFn: async (values: z.infer<typeof verifySchema>) =>
      call(() =>
        nexus.POST("/api/v1/me/email/verify", {
          body: { token: values.token.trim(), turnstile_token: verifyTurnstile.token ?? undefined },
        }),
      ),
    onSuccess: () => {
      toast.success(t("id.account.verifiedToast"));
      setSentTo(null);
      verifyForm.reset({ token: "" });
      void queryClient.invalidateQueries({ queryKey: ["me"] });
    },
    onSettled: () => verifyTurnstile.reset(),
  });

  if (!me.data) return null;
  const user = me.data;
  const roles = user.platform_roles.map((role) => maybeT(`roles.${role}`) ?? role).join(" · ");

  return (
    <>
      <h1 className="text-2xl font-semibold tracking-tight">{t("id.account.title")}</h1>
      <Card>
        <CardContent>
          <dl className="grid grid-cols-[112px_1fr] gap-y-3 text-sm">
            <dt className="text-muted-foreground">{t("id.account.login")}</dt>
            <dd className="font-medium">{user.user.login}</dd>
            <dt className="text-muted-foreground">{t("id.account.userId")}</dt>
            <dd className="font-mono text-xs">{user.user.id}</dd>
            <dt className="text-muted-foreground">{t("id.account.platformRoles")}</dt>
            <dd>{roles || t("id.account.none")}</dd>
            <dt className="text-muted-foreground">{t("id.account.personalOrg")}</dt>
            <dd>
              <Link className="underline underline-offset-3" href={`/o/${user.personal_organization_id}/overview`}>
                {t("id.account.openPersonal")}
              </Link>
            </dd>
            <dt className="text-muted-foreground">{t("id.account.email")}</dt>
            <dd>
              {user.verified_email ? (
                <span className="flex items-center gap-2">
                  {user.verified_email}
                  <StatusBadge tone="success">{t("id.account.verified")}</StatusBadge>
                </span>
              ) : (
                <span className="text-muted-foreground">{t("id.account.notBound")}</span>
              )}
            </dd>
          </dl>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>{t("id.account.bindTitle")}</CardTitle>
          <CardDescription>{t("id.account.bindSubtitle")}</CardDescription>
        </CardHeader>
        <CardContent>
          <form
            noValidate
            className="flex flex-col gap-4"
            onSubmit={bindForm.handleSubmit((values) => {
              if (needsTurnstile && !bindTurnstile.token) {
                bindForm.setError("root", { message: "turnstile" });
                return;
              }
              bind.mutate(values);
            })}
          >
            {options.data && !options.data.email_delivery_available ? (
              <Alert className="border-amber-200 bg-amber-50 text-amber-900">
                <AlertDescription className="text-amber-900">{t("id.account.deliveryUnavailable")}</AlertDescription>
              </Alert>
            ) : null}
            {bind.error ? <ErrorAlert error={bind.error} /> : null}
            {sentTo ? (
              <Alert>
                <CheckIcon />
                <AlertDescription>{t("id.account.sent", { email: sentTo })}</AlertDescription>
              </Alert>
            ) : null}
            <div className="grid gap-2">
              <Label htmlFor="bind-email">{t("id.account.newEmail")}</Label>
              <Input id="bind-email" type="email" autoComplete="email" aria-invalid={Boolean(bindForm.formState.errors.email) || undefined} {...bindForm.register("email")} />
              {bindForm.formState.errors.email ? <span className="text-xs text-destructive">{t("id.shared.emailInvalid")}</span> : null}
            </div>
            <div className="grid gap-2">
              <Label htmlFor="bind-password">{t("id.account.currentPassword")}</Label>
              <Input id="bind-password" type="password" autoComplete="current-password" {...bindForm.register("password")} />
            </div>
            {needsTurnstile && siteKey ? (
              <Turnstile siteKey={siteKey} action="bind_email" onToken={bindTurnstile.setToken} widgetRef={bindTurnstile.widget} />
            ) : null}
            {bindForm.formState.errors.root ? <span className="text-xs text-destructive">{t("id.shared.turnstileRequired")}</span> : null}
            <Button type="submit" className="self-start" disabled={bind.isPending}>
              {bind.isPending ? <Spinner /> : null}
              {bind.isPending ? t("id.account.binding") : t("id.account.bind")}
            </Button>
          </form>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>{t("id.account.verifyTitle")}</CardTitle>
          <CardDescription>{t("id.shared.tokenHint")}</CardDescription>
        </CardHeader>
        <CardContent>
          <form
            noValidate
            className="flex flex-col gap-4"
            onSubmit={verifyForm.handleSubmit((values) => {
              if (needsTurnstile && !verifyTurnstile.token) {
                verifyForm.setError("root", { message: "turnstile" });
                return;
              }
              verify.mutate(values);
            })}
          >
            {verify.error ? (
              <ErrorAlert
                error={verify.error}
                description={isApiError(verify.error) && verify.error.status === 401 ? t("id.shared.tokenInvalid") : undefined}
              />
            ) : null}
            <div className="grid gap-2">
              <Label htmlFor="verify-token">{t("id.account.token")}</Label>
              <Input id="verify-token" autoComplete="one-time-code" className="font-mono" aria-invalid={Boolean(verifyForm.formState.errors.token) || undefined} {...verifyForm.register("token")} />
              {verifyForm.formState.errors.token ? <span className="text-xs text-destructive">{t("id.shared.tokenRequired")}</span> : null}
            </div>
            {needsTurnstile && siteKey ? (
              <Turnstile siteKey={siteKey} action="confirm_email" onToken={verifyTurnstile.setToken} widgetRef={verifyTurnstile.widget} />
            ) : null}
            {verifyForm.formState.errors.root ? <span className="text-xs text-destructive">{t("id.shared.turnstileRequired")}</span> : null}
            <Button type="submit" className="self-start" disabled={verify.isPending}>
              {verify.isPending ? <Spinner /> : null}
              {verify.isPending ? t("id.account.verifying") : t("id.account.verify")}
            </Button>
          </form>
        </CardContent>
      </Card>
    </>
  );
}

export default function AccountPage() {
  return (
    <AccountFrame>
      <Suspense>
        <Account />
      </Suspense>
    </AccountFrame>
  );
}
