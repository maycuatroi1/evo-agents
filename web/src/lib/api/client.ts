import createClient, { type Client } from "openapi-fetch";

import { fromResponse, networkError } from "./errors";
import type { components, paths } from "./schema";

/**
 * The only way the web reads the hub API: a client typed from the API's OpenAPI document (`pnpm gen:api`).
 * Server components use `serverApi()` (forwards the session cookie to EVO_HUB_API_INTERNAL_URL); client
 * components use `browserApi()` (same origin, the browser sends the cookie).
 */
export type ApiClient = Client<paths>;

type Schemas = components["schemas"];
export type Project = Schemas["Project"];
export type WhoAmI = Schemas["WhoAmI"];
export type GrantInfo = Schemas["GrantInfo"];
export type Repo = Schemas["Repo"];
export type Sink = Schemas["Sink"];
export type AuthConfig = Schemas["AuthConfig"];
export type Role = "reader" | "writer" | "admin";

export type ClientOptions = {
  baseUrl: string;
  headers?: Record<string, string>;
  fetch?: (request: Request) => Promise<Response>;
};

export function createApiClient({ baseUrl, headers, fetch }: ClientOptions): ApiClient {
  return createClient<paths>({ baseUrl, headers, fetch });
}

type Result<T> = { data?: T; error?: unknown; response: Response };

/** The data of a call, or an ApiError: a JSON error body, a non-2xx answer, or no answer at all. */
export async function call<T>(pending: Promise<Result<T>>): Promise<T> {
  let result: Result<T>;
  try {
    result = await pending;
  } catch (cause) {
    if (cause instanceof DOMException && cause.name === "AbortError") throw cause;
    throw networkError(cause);
  }
  if (!result.response.ok || result.error !== undefined) throw fromResponse(result.response, result.error);
  return result.data as T;
}
