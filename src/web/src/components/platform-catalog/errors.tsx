"use client";

import { CircleAlertIcon } from "lucide-react";

import { TraceId, useErrorText } from "@/components/feedback";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { useI18n } from "@/i18n/provider";
import { isApiError } from "@/lib/api/errors";

/**
 * Error text for catalog and pricing pages: codes these pages know (pc.errorCodes.*) first,
 * including 503 mail_unavailable which the shared mapping would turn into a generic outage,
 * then the shared mapping.
 */
export function usePcErrorText() {
  const { maybeT } = useI18n();
  const shared = useErrorText();
  return (error: unknown): string => {
    if (isApiError(error) && error.status !== 412) {
      const known = maybeT(`pc.errorCodes.${error.code}`);
      if (known) return known;
    }
    return shared(error);
  };
}

/** ErrorAlert with page-specific error codes and the rejected field locations of a 422. */
export function PcErrorAlert({
  error,
  title,
  onRetry,
  className,
}: {
  error: unknown;
  title?: string;
  onRetry?: () => void;
  className?: string;
}) {
  const { t } = useI18n();
  const text = usePcErrorText()(error);
  const fields = isApiError(error) ? (error.fields ?? []) : [];
  return (
    <Alert variant="destructive" className={className}>
      <CircleAlertIcon />
      <AlertTitle>{title ?? t("errors.title")}</AlertTitle>
      <AlertDescription className="gap-2">
        <span>{text}</span>
        {fields.length > 0 ? (
          <span className="flex flex-col gap-0.5 text-xs">
            <span>{t("pc.common.fieldErrors")}</span>
            {fields.map((field, index) => (
              <code key={index} className="font-mono">
                {field.location.filter((part) => part !== "body").join(".")} · {field.code}
              </code>
            ))}
          </span>
        ) : null}
        <TraceId traceId={isApiError(error) ? error.traceId : undefined} />
        {onRetry ? (
          <Button type="button" variant="outline" size="sm" onClick={onRetry} className="mt-1">
            {t("common.retry")}
          </Button>
        ) : null}
      </AlertDescription>
    </Alert>
  );
}
