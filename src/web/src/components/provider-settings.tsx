"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2Icon, CircleAlertIcon, KeyRoundIcon, LockIcon, SendIcon } from "lucide-react";
import Link from "next/link";
import { useState, type ReactNode } from "react";
import { toast } from "sonner";

import { ConflictAlert, Spinner } from "@/components/feedback";
import { PcErrorAlert } from "@/components/platform-catalog/errors";
import { Field } from "@/components/platform-catalog/form";
import { StatusBadge } from "@/components/status-badge";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Button } from "@/components/ui/button";
import { Card, CardAction, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { useT } from "@/i18n/provider";
import { call, nexus } from "@/lib/api/client";
import { isConflict } from "@/lib/api/errors";
import type { components } from "@/lib/api/schema";
import { formatDateTime } from "@/lib/format";

type Schemas = components["schemas"];
export type MailPolicy = Schemas["MailPolicyView"];
export type TurnstilePolicy = Schemas["TurnstilePolicyView"];
type MailSettings = Schemas["MailSettings"];
type MailTestResult = Schemas["MailTestResult"];
type Provider = "none" | "smtp" | "resend" | "cloudflare";
type Switch = "registration" | "email_verification" | "turnstile";

export const MAIL_KEY = ["settings", "mail"] as const;
export const TURNSTILE_KEY = ["settings", "turnstile"] as const;
export const SETTINGS_LINKS = { mail: "/platform/settings?tab=mail", turnstile: "/platform/settings?tab=turnstile" };

const EMAIL = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
const HOST = /^(?=.{1,253}$)[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$/;
const ADDRESS = /^(\d{1,3}(\.\d{1,3}){3}|\[?[0-9A-Fa-f:]+\]?)$/;
const ACCOUNT = /^[0-9a-fA-F]{32}$/;
const SITE_KEY = /^[0-9A-Za-z_-]{1,255}$/;

/** GET /settings/mail: the identity mail provider without its secret, with the ETag. */
export function useMailPolicy() {
  return useQuery({ queryKey: MAIL_KEY, queryFn: async () => call(() => nexus.GET("/api/v1/settings/mail")), staleTime: 0 });
}

/** GET /settings/turnstile: the Turnstile keys without the secret, with the ETag. */
export function useTurnstilePolicy() {
  return useQuery({
    queryKey: TURNSTILE_KEY,
    queryFn: async () => call(() => nexus.GET("/api/v1/settings/turnstile")),
    staleTime: 0,
  });
}

/** Whether a provider is usable, and which authentication switches rely on it right now. */
export function ProviderStatusLine({
  available,
  requiredBy,
  keyVersion,
}: {
  available: boolean;
  requiredBy: Switch[];
  keyVersion: number | null;
}) {
  const t = useT();
  const names = requiredBy.map((name) => t(`st.provider.switches.${name}`)).join("、");
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 text-xs text-muted-foreground">
      <StatusBadge tone={available ? "success" : "warning"}>
        {available ? t("st.provider.available") : t("st.provider.unavailable")}
      </StatusBadge>
      <span>{requiredBy.length ? t("st.provider.requiredBy", { switches: names }) : t("st.provider.requiredByNone")}</span>
      {keyVersion !== null ? (
        <span className="inline-flex items-center gap-1">
          <KeyRoundIcon className="size-3" aria-hidden />
          {t("st.provider.keyVersion", { version: keyVersion })}
        </span>
      ) : null}
    </div>
  );
}

function ManagedAlert({ managed }: { managed: Schemas["ManagedSource"] | null }) {
  const t = useT();
  if (!managed) return null;
  return (
    <Alert>
      <LockIcon />
      <AlertTitle>{t("st.managed.title")}</AlertTitle>
      <AlertDescription>
        {t("st.managed.body", { digest: managed.file_sha256.slice(0, 12), at: formatDateTime(managed.imported_at) })}
      </AlertDescription>
    </Alert>
  );
}

/**
 * A write-only secret. A stored value shows as "set" and stays unless the operator chooses to
 * replace it; the stored value itself is never shown or sent back.
 */
function SecretField({
  id,
  label,
  hint,
  stored,
  editing,
  value,
  error,
  disabled,
  onEdit,
  onChange,
}: {
  id: string;
  label: string;
  hint?: string;
  stored: boolean;
  editing: boolean;
  value: string;
  error?: string;
  disabled?: boolean;
  onEdit: (editing: boolean) => void;
  onChange: (value: string) => void;
}) {
  const t = useT();
  if (stored && !editing) {
    return (
      <Field id={id} label={label} hint={t("st.provider.storedHint")}>
        <div className="flex items-center gap-2">
          <StatusBadge tone="success">{t("st.provider.stored")}</StatusBadge>
          <Button type="button" variant="outline" size="sm" disabled={disabled} onClick={() => onEdit(true)}>
            {t("st.provider.replace")}
          </Button>
        </div>
      </Field>
    );
  }
  return (
    <Field id={id} label={label} hint={hint} error={error}>
      <div className="flex items-center gap-2">
        <Input
          id={id}
          type="password"
          autoComplete="new-password"
          value={value}
          disabled={disabled}
          aria-invalid={error ? true : undefined}
          onChange={(event) => onChange(event.target.value)}
        />
        {stored ? (
          <Button
            type="button"
            variant="ghost"
            size="sm"
            disabled={disabled}
            onClick={() => {
              onChange("");
              onEdit(false);
            }}
          >
            {t("st.provider.keep")}
          </Button>
        ) : null}
      </div>
    </Field>
  );
}

interface MailForm {
  provider: Provider;
  sender: string;
  host: string;
  port: string;
  security: "starttls" | "tls";
  username: string;
  account: string;
  secret: string;
  editing: boolean;
}

function mailForm(view: MailPolicy): MailForm {
  return {
    provider: view.provider ?? "none",
    sender: view.sender ?? "",
    host: view.smtp?.host ?? "",
    port: String(view.smtp?.port ?? 587),
    security: view.smtp?.security ?? "starttls",
    username: view.smtp?.username ?? "",
    account: view.cloudflare?.account_id ?? "",
    secret: "",
    editing: false,
  };
}

/** Whether the stored view has a secret for this provider. */
function storedSecret(view: MailPolicy, provider: Provider) {
  if (provider !== view.provider) return false;
  return Boolean(view.smtp?.password_set || view.resend?.api_key_set || view.cloudflare?.api_token_set);
}

/**
 * The identity mail provider, edited as a whole: only the selected provider's fields are shown,
 * secrets are write-only, and a saved configuration can send a test message. Used on the
 * settings page and in the setup wizard.
 */
export function MailSettingsForm({
  view,
  etag,
  onSaved,
  onReload,
  reloading,
  compact = false,
}: {
  view: MailPolicy;
  etag: string;
  onSaved?: (view: MailPolicy) => void;
  onReload: () => void;
  reloading: boolean;
  /** One column, for the narrow setup wizard. */
  compact?: boolean;
}) {
  const t = useT();
  const queryClient = useQueryClient();
  const original = mailForm(view);
  const [form, setForm] = useState(original);
  const [touched, setTouched] = useState(false);
  const readOnly = view.managed !== null;
  const blocked = view.required_by.length > 0;
  const set = (changes: Partial<MailForm>) => setForm((current) => ({ ...current, ...changes }));
  const stored = storedSecret(view, form.provider);
  // A secret is sent only when typed; otherwise the stored one stays.
  const secretOpen = !stored || form.editing;
  const needsSecret =
    form.provider === "resend" || form.provider === "cloudflare" || (form.provider === "smtp" && form.username.trim() !== "");

  const port = Number(form.port);
  const problems = {
    sender: form.provider !== "none" && !EMAIL.test(form.sender.trim()),
    host: form.provider === "smtp" && !(HOST.test(form.host.trim()) || ADDRESS.test(form.host.trim())),
    port: form.provider === "smtp" && !(Number.isInteger(port) && port >= 1 && port <= 65535),
    account: form.provider === "cloudflare" && !ACCOUNT.test(form.account.trim()),
    secret: needsSecret && secretOpen && form.secret === "",
  };
  const valid = !Object.values(problems).some(Boolean);
  const changed = JSON.stringify({ ...form, editing: false }) !== JSON.stringify(original) || (form.editing && form.secret !== "");

  const body = (): MailSettings => {
    if (form.provider === "none") return { provider: null };
    const secret = secretOpen && form.secret !== "" ? form.secret : undefined;
    const sender = form.sender.trim();
    if (form.provider === "smtp") {
      const username = form.username.trim() || null;
      return {
        provider: "smtp",
        sender,
        smtp: {
          host: form.host.trim(),
          port,
          security: form.security,
          username,
          ...(username && secret !== undefined ? { password: secret } : {}),
        },
      };
    }
    if (form.provider === "resend") {
      return { provider: "resend", sender, resend: secret !== undefined ? { api_key: secret } : {} };
    }
    return {
      provider: "cloudflare",
      sender,
      cloudflare: { account_id: form.account.trim(), ...(secret !== undefined ? { api_token: secret } : {}) },
    };
  };

  const save = useMutation({
    mutationFn: async () =>
      call(() => nexus.PUT("/api/v1/settings/mail", { params: { header: { "If-Match": etag } }, body: body() })),
    onSuccess: (result) => {
      toast.success(t("st.saved"));
      queryClient.setQueryData(MAIL_KEY, result);
      void queryClient.invalidateQueries({ queryKey: ["setup"] });
      void queryClient.invalidateQueries({ queryKey: ["auth-options"] });
      onSaved?.(result.data);
    },
  });
  const conflict = isConflict(save.error);
  const secretLabel = {
    none: "",
    smtp: t("st.mail.smtp.password"),
    resend: t("st.mail.resend.api_key"),
    cloudflare: t("st.mail.cloudflare.api_token"),
  }[form.provider];
  const switchNames = view.required_by.map((name) => t(`st.provider.switches.${name}`)).join("、");

  return (
    <div className="grid gap-5">
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
      <fieldset className="grid gap-4" disabled={readOnly}>
        <div className={compact ? "grid gap-3" : "grid gap-x-4 gap-y-3 sm:grid-cols-2"}>
          <Field id="mail-provider" label={t("st.mail.provider")}>
            <Select
              value={form.provider}
              disabled={readOnly}
              onValueChange={(next) => set({ provider: next as Provider, secret: "", editing: false })}
            >
              <SelectTrigger id="mail-provider" className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {(["none", "smtp", "resend", "cloudflare"] as const).map((name) => (
                  <SelectItem key={name} value={name} disabled={name === "none" && blocked}>
                    {t(`st.mail.providers.${name}`)}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </Field>
          {form.provider !== "none" ? (
            <Field
              id="mail-sender"
              label={t("st.mail.sender")}
              hint={t("st.mail.senderHint")}
              error={touched && problems.sender ? t("id.shared.emailInvalid") : undefined}
            >
              <Input
                id="mail-sender"
                type="email"
                value={form.sender}
                aria-invalid={(touched && problems.sender) || undefined}
                onChange={(event) => set({ sender: event.target.value })}
              />
            </Field>
          ) : null}
        </div>
        {blocked && !readOnly ? (
          <p className="text-xs text-muted-foreground">
            {t("st.provider.removeBlocked", { switches: switchNames })}{" "}
            <Link href="/platform/users" className="underline underline-offset-4">
              {t("st.provider.switchesLink")}
            </Link>
          </p>
        ) : null}
        {form.provider === "smtp" ? (
          <div className={compact ? "grid grid-cols-2 gap-3" : "grid gap-x-4 gap-y-3 sm:grid-cols-2 lg:grid-cols-4"}>
            <Field
              id="smtp-host"
              label={t("st.mail.smtp.host")}
              error={touched && problems.host ? t("st.mail.invalidHost") : undefined}
              className={compact ? "col-span-2" : "lg:col-span-2"}
            >
              <Input id="smtp-host" value={form.host} onChange={(event) => set({ host: event.target.value })} />
            </Field>
            <Field id="smtp-port" label={t("st.mail.smtp.port")} error={touched && problems.port ? t("st.mail.invalidPort") : undefined}>
              <Input
                id="smtp-port"
                inputMode="numeric"
                className="tabular-nums"
                value={form.port}
                onChange={(event) => set({ port: event.target.value })}
              />
            </Field>
            <Field id="smtp-security" label={t("st.mail.smtp.security")} className={compact ? "col-span-2" : undefined}>
              <Select value={form.security} disabled={readOnly} onValueChange={(next) => set({ security: next as MailForm["security"] })}>
                <SelectTrigger id="smtp-security" className="w-full">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="starttls">{t("st.mail.smtp.securities.starttls")}</SelectItem>
                  <SelectItem value="tls">{t("st.mail.smtp.securities.tls")}</SelectItem>
                </SelectContent>
              </Select>
            </Field>
            <Field
              id="smtp-username"
              label={t("st.mail.smtp.username")}
              hint={t("st.mail.smtp.usernameHint")}
              className={compact ? "col-span-2" : "lg:col-span-2"}
            >
              <Input
                id="smtp-username"
                autoComplete="off"
                value={form.username}
                onChange={(event) => set({ username: event.target.value })}
              />
            </Field>
            {form.username.trim() ? (
              <div className={compact ? "col-span-2" : "lg:col-span-2"}>
                <SecretField
                  id="mail-secret"
                  label={secretLabel}
                  stored={stored}
                  editing={form.editing}
                  value={form.secret}
                  disabled={readOnly}
                  error={touched && problems.secret ? t("st.provider.secretRequired") : undefined}
                  onEdit={(editing) => set({ editing })}
                  onChange={(secret) => set({ secret })}
                />
              </div>
            ) : null}
          </div>
        ) : null}
        {form.provider === "resend" ? (
          <div className={compact ? "grid gap-3" : "grid gap-x-4 gap-y-3 sm:grid-cols-2"}>
            <SecretField
              id="mail-secret"
              label={secretLabel}
              stored={stored}
              editing={form.editing}
              value={form.secret}
              disabled={readOnly}
              error={touched && problems.secret ? t("st.provider.secretRequired") : undefined}
              onEdit={(editing) => set({ editing })}
              onChange={(secret) => set({ secret })}
            />
          </div>
        ) : null}
        {form.provider === "cloudflare" ? (
          <div className={compact ? "grid gap-3" : "grid gap-x-4 gap-y-3 sm:grid-cols-2"}>
            <Field
              id="cloudflare-account"
              label={t("st.mail.cloudflare.account_id")}
              hint={t("st.mail.cloudflare.account_idHint")}
              error={touched && problems.account ? t("st.mail.invalidAccount") : undefined}
            >
              <Input
                id="cloudflare-account"
                className="font-mono"
                value={form.account}
                onChange={(event) => set({ account: event.target.value })}
              />
            </Field>
            <SecretField
              id="mail-secret"
              label={secretLabel}
              hint={t("st.mail.cloudflare.api_tokenHint")}
              stored={stored}
              editing={form.editing}
              value={form.secret}
              disabled={readOnly}
              error={touched && problems.secret ? t("st.provider.secretRequired") : undefined}
              onEdit={(editing) => set({ editing })}
              onChange={(secret) => set({ secret })}
            />
          </div>
        ) : null}
      </fieldset>
      {readOnly ? null : (
        <div className="flex justify-end gap-2">
          <Button
            type="button"
            variant="outline"
            disabled={!changed || save.isPending}
            onClick={() => {
              setForm(original);
              setTouched(false);
            }}
          >
            {t("st.reset")}
          </Button>
          <Button
            type="button"
            disabled={!changed || save.isPending || conflict || reloading}
            onClick={() => {
              setTouched(true);
              if (valid) save.mutate();
            }}
          >
            {save.isPending ? <Spinner /> : null}
            {t("st.save")}
          </Button>
        </div>
      )}
      {view.provider !== null ? <MailTest available={view.available} dirty={changed} /> : null}
    </div>
  );
}

/** Send one message with the saved settings and show the provider's answer. */
function MailTest({ available, dirty }: { available: boolean; dirty: boolean }) {
  const t = useT();
  const [recipient, setRecipient] = useState("");
  const [result, setResult] = useState<{ to: string; outcome: MailTestResult } | null>(null);
  const send = useMutation({
    mutationFn: async (to: string) =>
      (await call(() => nexus.POST("/api/v1/settings/mail/test", { body: { recipient: to } }))).data,
    onMutate: () => setResult(null),
    onSuccess: (outcome, to) => setResult({ to, outcome }),
  });
  const address = recipient.trim();
  return (
    <div className="grid gap-3 rounded-md border border-dashed p-3">
      <div className="flex flex-col gap-0.5">
        <span className="text-sm font-medium">{t("st.mail.test.title")}</span>
        <span className="text-xs text-muted-foreground">{dirty ? t("st.mail.test.saveFirst") : t("st.mail.test.description")}</span>
      </div>
      <div className="flex flex-wrap items-end gap-2">
        <Field id="mail-test-recipient" label={t("st.mail.test.recipient")} className="min-w-[240px] flex-1">
          <Input
            id="mail-test-recipient"
            type="email"
            value={recipient}
            disabled={!available || dirty}
            onChange={(event) => setRecipient(event.target.value)}
          />
        </Field>
        <Button
          type="button"
          variant="outline"
          disabled={!available || dirty || send.isPending || !EMAIL.test(address)}
          onClick={() => send.mutate(address)}
        >
          {send.isPending ? <Spinner /> : <SendIcon />}
          {t("st.mail.test.send")}
        </Button>
      </div>
      {send.error ? <PcErrorAlert error={send.error} /> : null}
      {result?.outcome.accepted ? (
        <p className="flex items-center gap-1.5 text-sm text-emerald-700">
          <CheckCircle2Icon className="size-4" aria-hidden />
          {t("st.mail.test.accepted", { status: String(result.outcome.provider_status ?? "—"), email: result.to })}
        </p>
      ) : result ? (
        <Alert variant="destructive">
          <CircleAlertIcon />
          <AlertTitle>{t("st.mail.test.failed")}</AlertTitle>
          <AlertDescription>
            {result.outcome.provider_status !== null ? (
              <span>{t("st.mail.test.failedStatus", { status: String(result.outcome.provider_status) })}</span>
            ) : null}
            {result.outcome.detail ? <code className="font-mono text-xs break-all">{result.outcome.detail}</code> : null}
          </AlertDescription>
        </Alert>
      ) : null}
    </div>
  );
}

function SettingsCard({
  title,
  description,
  version,
  managed,
  status,
  children,
}: {
  title: ReactNode;
  description: ReactNode;
  version?: number;
  managed: Schemas["ManagedSource"] | null;
  status?: ReactNode;
  children: ReactNode;
}) {
  const t = useT();
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          {title}
          {managed ? <LockIcon className="size-4 text-muted-foreground" aria-label={t("st.managed.badge")} /> : null}
        </CardTitle>
        <CardDescription>{description}</CardDescription>
        {version !== undefined ? (
          <CardAction>
            <span className="text-xs text-muted-foreground tabular-nums">{t("st.version", { version })}</span>
          </CardAction>
        ) : null}
      </CardHeader>
      <CardContent className="grid gap-4">
        <ManagedAlert managed={managed} />
        {status}
        {children}
      </CardContent>
    </Card>
  );
}

function Loading() {
  return (
    <div className="grid gap-3 sm:grid-cols-2">
      <Skeleton className="h-16" />
      <Skeleton className="h-16" />
    </div>
  );
}

/** Settings → Mail: the identity mail provider. */
export function MailSettingsCard() {
  const t = useT();
  const query = useMailPolicy();
  const view = query.data?.data;
  return (
    <SettingsCard
      title={t("st.mail.title")}
      description={t("st.mail.description")}
      version={view?.version}
      managed={view?.managed ?? null}
      status={
        view ? <ProviderStatusLine available={view.available} requiredBy={view.required_by} keyVersion={view.key_version} /> : null
      }
    >
      {query.error && !view ? (
        <PcErrorAlert error={query.error} onRetry={() => void query.refetch()} />
      ) : view ? (
        <MailSettingsForm
          key={view.version}
          view={view}
          etag={query.data?.etag ?? `"${view.version}"`}
          reloading={query.isFetching}
          onReload={() => void query.refetch()}
        />
      ) : (
        <Loading />
      )}
    </SettingsCard>
  );
}

/** Settings → Bot protection: the Turnstile keys, checked with Siteverify before they are saved. */
export function TurnstileSettingsCard() {
  const t = useT();
  const query = useTurnstilePolicy();
  const site = useQuery({
    queryKey: ["settings", "site"],
    queryFn: async () => call(() => nexus.GET("/api/v1/settings/site")),
  });
  const view = query.data?.data;
  const consoleUrl = site.data?.data.console_url;
  let consoleHost: string | null = null;
  try {
    consoleHost = consoleUrl ? new URL(consoleUrl).hostname : null;
  } catch {
    consoleHost = null;
  }
  return (
    <SettingsCard
      title={t("st.turnstile.title")}
      description={t("st.turnstile.description")}
      version={view?.version}
      managed={view?.managed ?? null}
      status={
        view ? <ProviderStatusLine available={view.available} requiredBy={view.required_by} keyVersion={view.key_version} /> : null
      }
    >
      {query.error && !view ? (
        <PcErrorAlert error={query.error} onRetry={() => void query.refetch()} />
      ) : view ? (
        <TurnstileForm
          key={view.version}
          view={view}
          etag={query.data?.etag ?? `"${view.version}"`}
          consoleHost={consoleHost}
          reloading={query.isFetching}
          onReload={() => void query.refetch()}
        />
      ) : (
        <Loading />
      )}
    </SettingsCard>
  );
}

function TurnstileForm({
  view,
  etag,
  consoleHost,
  reloading,
  onReload,
}: {
  view: TurnstilePolicy;
  etag: string;
  consoleHost: string | null;
  reloading: boolean;
  onReload: () => void;
}) {
  const t = useT();
  const queryClient = useQueryClient();
  const readOnly = view.managed !== null;
  const stored = view.secret_key_set;
  const [siteKey, setSiteKey] = useState(view.site_key ?? "");
  const [hostname, setHostname] = useState(view.hostname ?? "");
  const [secret, setSecret] = useState("");
  const [editing, setEditing] = useState(false);
  const [touched, setTouched] = useState(false);
  const [removing, setRemoving] = useState(false);
  const secretOpen = !stored || editing;
  const host = hostname.trim().toLowerCase();
  const problems = {
    siteKey: !SITE_KEY.test(siteKey.trim()),
    hostname: host !== "" && !HOST.test(host),
    noHost: host === "" && !consoleHost,
    secret: secretOpen && secret === "",
  };
  const valid = !Object.values(problems).some(Boolean);
  const changed =
    siteKey !== (view.site_key ?? "") || hostname !== (view.hostname ?? "") || (secretOpen && secret !== "");
  const blocked = view.required_by.length > 0;

  const write = useMutation({
    mutationFn: async (body: Schemas["TurnstileSettings"]) =>
      call(() => nexus.PUT("/api/v1/settings/turnstile", { params: { header: { "If-Match": etag } }, body })),
    onSuccess: (result, body) => {
      toast.success(body.site_key === null ? t("st.turnstile.removed") : t("st.saved"));
      queryClient.setQueryData(TURNSTILE_KEY, result);
      void queryClient.invalidateQueries({ queryKey: ["auth-options"] });
    },
  });
  const conflict = isConflict(write.error);
  const verifying = write.isPending && write.variables?.secret_key;

  return (
    <div className="grid gap-5">
      {conflict ? (
        <ConflictAlert
          onReload={() => {
            write.reset();
            onReload();
          }}
        />
      ) : write.error ? (
        <PcErrorAlert error={write.error} />
      ) : null}
      <fieldset className="grid gap-x-4 gap-y-3 sm:grid-cols-2" disabled={readOnly}>
        <Field
          id="turnstile-site-key"
          label={t("st.turnstile.site_key")}
          hint={t("st.turnstile.site_keyHint")}
          error={touched && problems.siteKey ? t("st.turnstile.invalidSiteKey") : undefined}
        >
          <Input id="turnstile-site-key" className="font-mono" value={siteKey} onChange={(event) => setSiteKey(event.target.value)} />
        </Field>
        <SecretField
          id="turnstile-secret"
          label={t("st.turnstile.secret_key")}
          stored={stored}
          editing={editing}
          value={secret}
          disabled={readOnly}
          error={touched && problems.secret ? t("st.provider.secretRequired") : undefined}
          onEdit={setEditing}
          onChange={setSecret}
        />
        <Field
          id="turnstile-hostname"
          label={t("st.turnstile.hostname")}
          hint={t("st.turnstile.hostnameHint")}
          error={
            touched && problems.hostname
              ? t("st.turnstile.invalidHost")
              : touched && problems.noHost
                ? t("st.turnstile.noHost")
                : undefined
          }
        >
          <Input
            id="turnstile-hostname"
            value={hostname}
            placeholder={consoleHost ? t("st.turnstile.hostnamePlaceholder", { host: consoleHost }) : undefined}
            onChange={(event) => setHostname(event.target.value)}
          />
        </Field>
        {view.effective_hostname ? (
          <p className="self-end pb-2 text-xs text-muted-foreground">
            {t("st.turnstile.effective", { host: view.effective_hostname })}
          </p>
        ) : null}
      </fieldset>
      {verifying ? (
        <p className="flex items-center gap-1.5 text-xs text-muted-foreground">
          <Spinner />
          {t("st.turnstile.verifying")}
        </p>
      ) : null}
      {blocked && !readOnly && view.site_key ? (
        <p className="text-xs text-muted-foreground">
          {t("st.provider.removeBlocked", { switches: t("st.provider.switches.turnstile") })}{" "}
          <Link href="/platform/users" className="underline underline-offset-4">
            {t("st.provider.switchesLink")}
          </Link>
        </p>
      ) : null}
      {readOnly ? null : (
        <div className="flex flex-wrap justify-between gap-2">
          <Button
            type="button"
            variant="outline"
            disabled={!view.site_key || blocked || write.isPending}
            onClick={() => setRemoving(true)}
          >
            {t("st.turnstile.remove")}
          </Button>
          <div className="flex gap-2">
            <Button
              type="button"
              variant="outline"
              disabled={!changed || write.isPending}
              onClick={() => {
                setSiteKey(view.site_key ?? "");
                setHostname(view.hostname ?? "");
                setSecret("");
                setEditing(false);
                setTouched(false);
              }}
            >
              {t("st.reset")}
            </Button>
            <Button
              type="button"
              disabled={!changed || write.isPending || conflict || reloading}
              onClick={() => {
                setTouched(true);
                if (!valid) return;
                write.mutate({
                  site_key: siteKey.trim(),
                  hostname: host || null,
                  ...(secretOpen && secret !== "" ? { secret_key: secret } : {}),
                });
              }}
            >
              {write.isPending ? <Spinner /> : null}
              {t("st.save")}
            </Button>
          </div>
        </div>
      )}
      <AlertDialog open={removing} onOpenChange={setRemoving}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t("st.turnstile.remove")}</AlertDialogTitle>
            <AlertDialogDescription>{t("st.turnstile.removeConfirm")}</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{t("common.cancel")}</AlertDialogCancel>
            <AlertDialogAction onClick={() => write.mutate({ site_key: null })}>{t("st.turnstile.remove")}</AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
