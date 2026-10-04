/**
 * What a failed API call turns into. The hub answers every error with `{error, message, request_id}` (see
 * `evo_agents.hub.server.errors.ErrorBody`); a failure before any answer (API down, DNS, timeout) has status 0.
 */

export type ApiErrorInfo = {
  /** HTTP status, or 0 when no answer arrived. */
  status: number;
  /** The API's stable code (`forbidden`, `not_found`, ...), or `network`. */
  code: string;
  /** The API's message, in English; pages show their own copy and keep this as a detail. */
  message: string;
  /** X-Request-ID of the failed call, to quote when reporting a 5xx. */
  requestId: string | null;
};

export type ErrorKind = "unauthorized" | "forbidden" | "not_found" | "server" | "network" | "client";

export class ApiError extends Error {
  readonly info: ApiErrorInfo;

  constructor(info: ApiErrorInfo) {
    super(`${info.status || "network"} ${info.code}: ${info.message}`);
    this.name = "ApiError";
    this.info = info;
  }

  get status(): number {
    return this.info.status;
  }

  get kind(): ErrorKind {
    return errorKind(this.info.status);
  }
}

export function errorKind(status: number): ErrorKind {
  if (status === 0) return "network";
  if (status === 401) return "unauthorized";
  if (status === 403) return "forbidden";
  if (status === 404) return "not_found";
  if (status >= 500) return "server";
  return "client";
}

function field(body: unknown, key: string): string | null {
  if (typeof body !== "object" || body === null || !(key in body)) return null;
  const value = (body as Record<string, unknown>)[key];
  return typeof value === "string" && value.length > 0 ? value : null;
}

/** The error of an answered call, from its JSON body when it has one and its headers otherwise. */
export function fromResponse(response: Response, body: unknown): ApiError {
  return new ApiError({
    status: response.status,
    code: field(body, "error") ?? `http_${response.status}`,
    message: field(body, "message") ?? (response.statusText || `HTTP ${response.status}`),
    requestId: field(body, "request_id") ?? response.headers.get("x-request-id"),
  });
}

export function networkError(cause: unknown): ApiError {
  const message = cause instanceof Error ? cause.message : String(cause);
  return new ApiError({ status: 0, code: "network", message, requestId: null });
}

export function isApiError(value: unknown): value is ApiError {
  return value instanceof ApiError;
}

/** A plain object a server component can hand to a client component. */
export function toInfo(error: unknown): ApiErrorInfo {
  return isApiError(error) ? { ...error.info } : networkError(error).info;
}
