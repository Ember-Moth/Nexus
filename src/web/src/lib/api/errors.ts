/** Error envelope returned by the Nexus API and by the BFF: {error:{code,message,trace_id}}. */
export interface ErrorEnvelope {
  error: {
    code: string;
    message: string;
    trace_id?: string;
    fields?: { location: (string | number)[]; code: string }[];
  };
}

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly traceId: string | undefined;
  readonly fields: ErrorEnvelope["error"]["fields"];
  readonly retryAfter: number | undefined;

  constructor(init: {
    status: number;
    code: string;
    message: string;
    traceId?: string;
    fields?: ErrorEnvelope["error"]["fields"];
    retryAfter?: number;
  }) {
    super(init.message);
    this.name = "ApiError";
    this.status = init.status;
    this.code = init.code;
    this.traceId = init.traceId;
    this.fields = init.fields;
    this.retryAfter = init.retryAfter;
  }

  static fromResponse(response: Response, body: unknown): ApiError {
    const envelope = isEnvelope(body) ? body.error : undefined;
    const retryAfter = Number(response.headers.get("retry-after"));
    return new ApiError({
      status: response.status,
      code: envelope?.code ?? `http_${response.status}`,
      message: envelope?.message ?? response.statusText ?? "Request failed",
      traceId: envelope?.trace_id ?? response.headers.get("x-request-id") ?? undefined,
      fields: envelope?.fields,
      retryAfter: Number.isFinite(retryAfter) && retryAfter > 0 ? retryAfter : undefined,
    });
  }

  static network(cause: unknown): ApiError {
    const error = new ApiError({ status: 0, code: "network_error", message: "Network request failed" });
    error.cause = cause;
    return error;
  }
}

function isEnvelope(value: unknown): value is ErrorEnvelope {
  return (
    typeof value === "object" &&
    value !== null &&
    "error" in value &&
    typeof (value as ErrorEnvelope).error?.code === "string"
  );
}

export const isApiError = (error: unknown): error is ApiError => error instanceof ApiError;
export const isConflict = (error: unknown) => isApiError(error) && error.status === 412;
export const isNotFound = (error: unknown) =>
  isApiError(error) && (error.status === 404 || error.status === 403);

/**
 * Whether repeating the same request (with the same Idempotency-Key) is reasonable:
 * the outcome is unknown (network failure, 502/503/504) or the server asked us to slow down.
 */
export const isRetryable = (error: unknown) =>
  isApiError(error) && (error.status === 0 || error.status === 429 || error.status >= 502);
