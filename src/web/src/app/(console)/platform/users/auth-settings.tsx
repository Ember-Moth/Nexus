"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2Icon, CircleAlertIcon, ShieldCheckIcon } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";

import { useAuthOptions } from "@/components/auth-layout";
import { ConflictAlert, Spinner } from "@/components/feedback";
import { PcErrorAlert } from "@/components/platform-catalog/errors";
import { SwitchRow } from "@/components/platform-catalog/form";
import type { AuthPolicyView } from "@/components/platform-catalog/types";
import { Button } from "@/components/ui/button";
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { useT } from "@/i18n/provider";
import { call, nexus } from "@/lib/api/client";
import { isConflict } from "@/lib/api/errors";

const settingsKey = ["settings", "authentication"] as const;

/**
 * GET /settings/authentication for the switches and the ETag, PUT with If-Match. Readiness hints
 * come from the public GET /auth/options (mail delivery; Turnstile keys are only reported once
 * enabled, so a missing key surfaces as the API's 409).
 */
export function AuthSettingsCard() {
  const t = useT();
  const settings = useQuery({
    queryKey: settingsKey,
    queryFn: async () => call(() => nexus.GET("/api/v1/settings/authentication")),
    staleTime: 0,
  });
  const policy = settings.data?.data;

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <ShieldCheckIcon className="size-4 text-muted-foreground" aria-hidden />
          {t("pc.auth.title")}
        </CardTitle>
        <CardDescription>{t("pc.auth.description")}</CardDescription>
        {policy ? (
          <CardAction>
            <span className="text-xs text-muted-foreground tabular-nums">{t("pc.auth.version", { version: policy.version })}</span>
          </CardAction>
        ) : null}
      </CardHeader>
      <CardContent>
        {settings.error && !policy ? (
          <PcErrorAlert error={settings.error} onRetry={() => void settings.refetch()} />
        ) : policy ? (
          <AuthSettingsForm
            key={policy.version}
            policy={policy}
            etag={settings.data?.etag ?? `"${policy.version}"`}
            reloading={settings.isFetching}
            onReload={() => void settings.refetch()}
          />
        ) : (
          <div className="grid gap-3">
            <Skeleton className="h-16" />
            <Skeleton className="h-16" />
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function AuthSettingsForm({
  policy,
  etag,
  reloading,
  onReload,
}: {
  policy: AuthPolicyView;
  etag: string;
  reloading: boolean;
  onReload: () => void;
}) {
  const t = useT();
  const queryClient = useQueryClient();
  const options = useAuthOptions();
  const [registration, setRegistration] = useState(policy.registration);
  const [emailVerification, setEmailVerification] = useState(policy.email_verification);
  const [turnstile, setTurnstile] = useState(policy.turnstile);

  // The PUT replaces the whole policy: limits and lifetimes travel along unchanged.
  const managed = policy.managed;
  const locked = managed !== null;
  const rest = Object.fromEntries(
    Object.entries(policy).filter(([key]) => key !== "version" && key !== "managed"),
  ) as Omit<AuthPolicyView, "version" | "managed">;
  const save = useMutation({
    mutationFn: async (switches: { registration: boolean; email_verification: boolean; turnstile: boolean }) =>
      call(() =>
        nexus.PUT("/api/v1/settings/authentication", {
          params: { header: { "If-Match": etag } },
          body: { ...rest, ...switches },
        }),
      ),
    onSuccess: () => {
      toast.success(t("pc.auth.saved"));
      void queryClient.invalidateQueries({ queryKey: settingsKey });
      void queryClient.invalidateQueries({ queryKey: ["auth-options"] });
    },
  });

  const changed =
    registration !== policy.registration ||
    emailVerification !== policy.email_verification ||
    turnstile !== policy.turnstile;
  const conflict = isConflict(save.error);
  const mailReady = options.data?.email_delivery_available;

  return (
    <div className="grid gap-3">
      {conflict ? (
        <ConflictAlert
          onReload={() => {
            save.reset();
            onReload();
          }}
        />
      ) : save.error ? (
        <PcErrorAlert error={save.error} />
      ) : null}
      {locked ? <p className="text-xs text-muted-foreground">{t("st.managed.title")}</p> : null}
      <SwitchRow
        id="auth-registration"
        label={t("pc.auth.registration")}
        hint={t("pc.auth.registrationHint")}
        checked={registration}
        disabled={locked}
        onChange={(next) => {
          setRegistration(next);
          // The API refuses open registration without email verification.
          if (next) setEmailVerification(true);
        }}
      >
        {registration && !policy.email_verification ? <Readiness ok text={t("pc.auth.registrationEnablesEmail")} /> : null}
      </SwitchRow>
      <SwitchRow
        id="auth-email-verification"
        label={t("pc.auth.emailVerification")}
        hint={registration ? t("pc.auth.emailVerificationLocked") : t("pc.auth.emailVerificationHint")}
        checked={emailVerification}
        disabled={registration || locked}
        onChange={setEmailVerification}
      >
        {options.data ? (
          <Readiness ok={Boolean(mailReady)} text={mailReady ? t("pc.auth.mailReady") : t("pc.auth.mailMissing")} />
        ) : options.error ? (
          <Readiness ok={false} text={t("pc.auth.optionsUnavailable")} />
        ) : null}
      </SwitchRow>
      <SwitchRow
        id="auth-turnstile"
        label={t("pc.auth.turnstile")}
        hint={t("pc.auth.turnstileHint")}
        checked={turnstile}
        disabled={locked}
        onChange={setTurnstile}
      >
        {options.data?.turnstile && options.data.turnstile_site_key ? (
          <Readiness ok text={t("pc.auth.turnstileActive")} />
        ) : null}
      </SwitchRow>
      <div className="flex justify-end">
        <Button
          type="button"
          disabled={!changed || save.isPending || conflict || reloading || locked}
          onClick={() => save.mutate({ registration, email_verification: emailVerification, turnstile })}
        >
          {save.isPending ? <Spinner /> : null}
          {t("pc.auth.save")}
        </Button>
      </div>
    </div>
  );
}

function Readiness({ ok, text }: { ok: boolean; text: string }) {
  return (
    <span className={ok ? "flex items-center gap-1 text-xs text-emerald-700" : "flex items-center gap-1 text-xs text-amber-700"}>
      {ok ? <CheckCircle2Icon className="size-3.5" aria-hidden /> : <CircleAlertIcon className="size-3.5" aria-hidden />}
      {text}
    </span>
  );
}
