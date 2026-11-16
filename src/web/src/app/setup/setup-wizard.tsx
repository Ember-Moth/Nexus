"use client";

import { useQueryClient } from "@tanstack/react-query";
import { CheckIcon, LockIcon, TriangleAlertIcon } from "lucide-react";
import { useRouter } from "next/navigation";
import { useEffect, useState, type FormEvent } from "react";
import { toast } from "sonner";

import { AuthLayout } from "@/components/auth-layout";
import { ErrorAlert, Spinner } from "@/components/feedback";
import { Field } from "@/components/platform-catalog/form";
import { useSignOut } from "@/components/shell/user-menu";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useT } from "@/i18n/provider";
import { call, nexus } from "@/lib/api/client";
import { ApiError, isApiError } from "@/lib/api/errors";
import { useSetupStatus } from "@/lib/api/hooks";
import type { SetupStatus, SiteInfo } from "@/lib/api/types";
import { cn } from "@/lib/utils";

const DEFAULT_EMAIL = "admin@example.com";
const DEFAULT_PASSWORD = "password";
// Documentation domains the server refuses for the administrator (RFC 2606 and RFC 6761).
const RESERVED_DOMAINS = ["example.com", "example.net", "example.org", "example", "test", "invalid", "localhost", "local"];
const LOOPBACK = new Set(["localhost", "127.0.0.1", "[::1]"]);
const STEPS = ["site", "email", "password"] as const;
type Step = 0 | 1 | 2;

const isEmail = (value: string) => /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value);

function reservedEmail(value: string) {
  const address = value.trim().toLowerCase();
  const domain = address.split("@").pop() ?? "";
  return address === DEFAULT_EMAIL || RESERVED_DOMAINS.some((name) => domain === name || domain.endsWith(`.${name}`));
}

/** Mirrors the API: HTTPS, or HTTP on a loopback host; no credentials, query or fragment. */
function consoleUrlValid(value: string) {
  try {
    const url = new URL(value);
    if (!["https:", "http:"].includes(url.protocol) || url.username || url.password || url.search || url.hash) return false;
    return url.protocol === "https:" || LOOPBACK.has(url.hostname) || url.hostname.endsWith(".localhost");
  } catch {
    return false;
  }
}

/** The step a rejected submission belongs to, from its error code or field location. */
function stepOf(error: ApiError): Step | null {
  if (["email_reserved", "invalid_email", "email_in_use", "email_verification_required", "invalid_verification_token"].includes(error.code)) {
    return 1;
  }
  if (error.code === "policy_managed_externally") return 0;
  if (error.code === "invalid_password") return 2;
  const field = error.fields?.[0]?.location[1];
  if (field === "site") return 0;
  if (field === "email" || field === "email_token") return 1;
  if (field === "password") return 2;
  return null;
}

/** Signed-in entry point: sends a finished installation home, explains the wizard to others. */
export function SetupWizard() {
  const t = useT();
  const router = useRouter();
  const status = useSetupStatus();
  const { signOut, pending } = useSignOut();
  const finished = status.data?.required === false;

  useEffect(() => {
    if (finished) router.replace("/");
  }, [finished, router]);

  let body;
  if (status.error) {
    body = <ErrorAlert error={status.error} onRetry={() => void status.refetch()} />;
  } else if (!status.data || finished) {
    body = (
      <div className="flex justify-center py-10 text-muted-foreground">
        <Spinner />
      </div>
    );
  } else if (!status.data.builtin_admin) {
    body = (
      <Alert>
        <LockIcon />
        <AlertTitle>{t("id.wizard.notBuiltinTitle")}</AlertTitle>
        <AlertDescription>{t("id.wizard.notBuiltinBody")}</AlertDescription>
      </Alert>
    );
  } else {
    body = <WizardForm status={status.data} />;
  }

  return (
    <AuthLayout
      footer={
        <Button variant="link" size="sm" className="text-xs text-muted-foreground" disabled={pending} onClick={() => void signOut()}>
          {t("id.wizard.signOut")}
        </Button>
      }
    >
      <div className="flex w-full max-w-[480px] flex-col gap-5 rounded-2xl border bg-card p-7 shadow-sm">
        <div className="flex flex-col gap-1.5">
          <h1 className="text-[22px] font-semibold tracking-tight">{t("id.wizard.title")}</h1>
          <p className="text-sm text-muted-foreground">{t("id.wizard.subtitle")}</p>
        </div>
        {body}
      </div>
    </AuthLayout>
  );
}

function Steps({ current }: { current: Step }) {
  const t = useT();
  return (
    <ol className="grid grid-cols-3 gap-2" aria-label={t("id.wizard.stepOf", { current: current + 1, total: STEPS.length })}>
      {STEPS.map((name, index) => {
        const done = index < current;
        const active = index === current;
        return (
          <li key={name} aria-current={active ? "step" : undefined} className="flex flex-col gap-1.5">
            <span className={cn("h-1 rounded-full", done || active ? "bg-primary" : "bg-muted")} />
            <span className={cn("flex items-center gap-1 text-xs", active ? "font-medium text-foreground" : "text-muted-foreground")}>
              {done ? <CheckIcon className="size-3" aria-hidden /> : <span className="tabular-nums">{index + 1}.</span>}
              {t(`id.wizard.steps.${name}`)}
            </span>
          </li>
        );
      })}
    </ol>
  );
}

function WizardForm({ status }: { status: SetupStatus }) {
  const t = useT();
  const router = useRouter();
  const queryClient = useQueryClient();
  const managed = status.site.managed !== null;
  const mailed = status.email_delivery_available;
  const [step, setStep] = useState<Step>(0);
  const [touched, setTouched] = useState(false);
  const [site, setSite] = useState({
    site_name: status.site.site_name,
    // A first visit proposes the address the browser is using right now.
    console_url: status.site.console_url ?? (managed ? "" : window.location.origin),
    contact_email: status.site.contact_email ?? "",
  });
  const [email, setEmail] = useState("");
  const [sentTo, setSentTo] = useState<string | null>(null);
  const [token, setToken] = useState("");
  const [sending, setSending] = useState(false);
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const address = email.trim();
  const problems = {
    site_name: !site.site_name.trim() || site.site_name.trim().length > 100,
    console_url: site.console_url.trim() !== "" && !consoleUrlValid(site.console_url.trim()),
    contact_email: site.contact_email.trim() !== "" && !isEmail(site.contact_email.trim()),
    email: !isEmail(address) ? "id.shared.emailInvalid" : reservedEmail(address) ? "id.wizard.email.reserved" : null,
    token: mailed && (sentTo !== address || !token.trim()),
    password:
      password.length < 16 || password.length > 1024
        ? "id.shared.passwordTooShort"
        : password === DEFAULT_PASSWORD
          ? "id.wizard.password.notDefault"
          : null,
    confirm: confirm !== password,
  } as const;
  const stepValid = [
    managed || !(problems.site_name || problems.console_url || problems.contact_email),
    !problems.email && !problems.token,
    !problems.password && !problems.confirm,
  ];

  const siteBody = (): SiteInfo => ({
    site_name: site.site_name.trim(),
    console_url: site.console_url.trim() || null,
    contact_email: site.contact_email.trim() || null,
  });

  const go = (next: Step) => {
    setError(null);
    setTouched(false);
    setStep(next);
  };

  const sendToken = async () => {
    setTouched(true);
    if (problems.email) return;
    setSending(true);
    setError(null);
    try {
      await call(() => nexus.POST("/api/v1/setup/email-verification", { body: { email: address } }));
      setSentTo(address);
      setToken("");
    } catch (cause) {
      setError(cause);
    } finally {
      setSending(false);
    }
  };

  const finish = async () => {
    setSubmitting(true);
    setError(null);
    try {
      const result = (
        await call(() =>
          nexus.POST("/api/v1/setup", {
            body: {
              // A managed site description is sent back exactly as the policy file set it.
              site: managed
                ? { site_name: status.site.site_name, console_url: status.site.console_url, contact_email: status.site.contact_email }
                : siteBody(),
              email: address,
              email_token: mailed ? token.trim() : null,
              password,
            },
          }),
        )
      ).data;
      queryClient.setQueryData(["setup"], result);
      void queryClient.invalidateQueries({ queryKey: ["me"] });
      toast.success(t("id.wizard.done"));
      router.replace("/");
    } catch (cause) {
      setSubmitting(false);
      if (isApiError(cause) && cause.code === "setup_completed") {
        void queryClient.invalidateQueries({ queryKey: ["setup"] });
        return;
      }
      if (isApiError(cause) && cause.code === "policy_managed_externally") {
        void queryClient.invalidateQueries({ queryKey: ["setup"] });
      }
      const target = isApiError(cause) ? stepOf(cause) : null;
      if (target !== null) setStep(target);
      setError(cause);
    }
  };

  const onSubmit = (event: FormEvent) => {
    event.preventDefault();
    setTouched(true);
    if (!stepValid[step]) return;
    if (step < 2) go((step + 1) as Step);
    else void finish();
  };

  return (
    <form onSubmit={onSubmit} noValidate className="flex flex-col gap-5">
      <Steps current={step} />
      {error ? <ErrorAlert error={error} /> : null}

      {step === 0 ? (
        <div className="flex flex-col gap-4">
          {managed ? (
            <Alert>
              <LockIcon />
              <AlertTitle>{t("id.wizard.site.managedTitle")}</AlertTitle>
              <AlertDescription>{t("id.wizard.site.managedBody")}</AlertDescription>
            </Alert>
          ) : null}
          <Field
            id="site-name"
            label={t("id.wizard.site.name")}
            hint={t("id.wizard.site.nameHint")}
            error={touched && problems.site_name && !managed ? t("id.wizard.site.nameRequired") : undefined}
          >
            <Input
              id="site-name"
              autoFocus
              disabled={managed}
              maxLength={100}
              value={site.site_name}
              aria-invalid={(touched && problems.site_name && !managed) || undefined}
              onChange={(event) => setSite({ ...site, site_name: event.target.value })}
            />
          </Field>
          <Field
            id="console-url"
            label={t("id.wizard.site.consoleUrl")}
            hint={t("id.wizard.site.consoleUrlHint")}
            error={touched && problems.console_url && !managed ? t("id.wizard.site.consoleUrlInvalid") : undefined}
          >
            <Input
              id="console-url"
              type="url"
              inputMode="url"
              disabled={managed}
              placeholder="https://console.example.com"
              value={site.console_url}
              aria-invalid={(touched && problems.console_url && !managed) || undefined}
              onChange={(event) => setSite({ ...site, console_url: event.target.value })}
            />
          </Field>
          <Field
            id="contact-email"
            label={t("id.wizard.site.contactEmail")}
            hint={t("id.wizard.site.contactEmailHint")}
            error={touched && problems.contact_email && !managed ? t("id.shared.emailInvalid") : undefined}
          >
            <Input
              id="contact-email"
              type="email"
              autoComplete="email"
              disabled={managed}
              value={site.contact_email}
              aria-invalid={(touched && problems.contact_email && !managed) || undefined}
              onChange={(event) => setSite({ ...site, contact_email: event.target.value })}
            />
          </Field>
        </div>
      ) : null}

      {step === 1 ? (
        <div className="flex flex-col gap-4">
          <p className="text-sm text-muted-foreground">{t("id.wizard.email.current", { email: DEFAULT_EMAIL })}</p>
          <Field
            id="admin-email"
            label={t("id.wizard.email.address")}
            error={touched && problems.email ? t(problems.email) : undefined}
          >
            <div className="flex gap-2">
              <Input
                id="admin-email"
                type="email"
                autoComplete="email"
                autoFocus
                value={email}
                aria-invalid={(touched && Boolean(problems.email)) || undefined}
                onChange={(event) => setEmail(event.target.value)}
              />
              {mailed ? (
                <Button type="button" variant="outline" disabled={sending} onClick={() => void sendToken()}>
                  {sending ? <Spinner /> : null}
                  {sentTo === address ? t("id.wizard.email.resend") : t("id.wizard.email.send")}
                </Button>
              ) : null}
            </div>
          </Field>
          {mailed ? (
            sentTo === address ? (
              <>
                <Alert>
                  <AlertDescription>{t("id.wizard.email.sent", { email: sentTo })}</AlertDescription>
                </Alert>
                <Field
                  id="email-token"
                  label={t("id.wizard.email.token")}
                  error={touched && problems.token ? t("id.wizard.email.tokenRequired") : undefined}
                >
                  <Input
                    id="email-token"
                    autoComplete="one-time-code"
                    className="font-mono"
                    value={token}
                    aria-invalid={(touched && problems.token) || undefined}
                    onChange={(event) => setToken(event.target.value)}
                  />
                </Field>
              </>
            ) : touched && !problems.email ? (
              <p className="-mt-2 text-sm text-destructive">{t("id.wizard.email.tokenRequired")}</p>
            ) : null
          ) : (
            <Alert className="border-amber-200 bg-amber-50 text-amber-900">
              <TriangleAlertIcon />
              <AlertTitle>{t("id.wizard.email.unverifiedTitle")}</AlertTitle>
              <AlertDescription className="text-amber-900">{t("id.wizard.email.unverifiedBody")}</AlertDescription>
            </Alert>
          )}
        </div>
      ) : null}

      {step === 2 ? (
        <div className="flex flex-col gap-4">
          <Field
            id="new-password"
            label={t("id.wizard.password.password")}
            hint={t("id.shared.passwordHint")}
            error={touched && problems.password ? t(problems.password) : undefined}
          >
            <Input
              id="new-password"
              type="password"
              autoComplete="new-password"
              autoFocus
              value={password}
              aria-invalid={(touched && Boolean(problems.password)) || undefined}
              onChange={(event) => setPassword(event.target.value)}
            />
          </Field>
          <Field
            id="confirm-password"
            label={t("id.wizard.password.confirm")}
            error={touched && !problems.password && problems.confirm ? t("id.shared.passwordMismatch") : undefined}
          >
            <Input
              id="confirm-password"
              type="password"
              autoComplete="new-password"
              value={confirm}
              aria-invalid={(touched && problems.confirm) || undefined}
              onChange={(event) => setConfirm(event.target.value)}
            />
          </Field>
          <p className="text-xs text-muted-foreground">{t("id.wizard.password.effect")}</p>
        </div>
      ) : null}

      <div className="flex justify-between gap-2">
        <Button type="button" variant="outline" disabled={step === 0 || submitting} onClick={() => go((step - 1) as Step)}>
          {t("id.wizard.back")}
        </Button>
        <Button type="submit" disabled={submitting}>
          {submitting ? <Spinner /> : null}
          {step < 2 ? t("id.wizard.next") : submitting ? t("id.wizard.submitting") : t("id.wizard.submit")}
        </Button>
      </div>
    </form>
  );
}
