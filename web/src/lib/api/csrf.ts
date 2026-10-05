import { type ApiClient, call } from "./client";

/**
 * The header a write made with the session cookie must carry: the session's X-Evo-CSRF token, fetched just before
 * the write (the hub answers 403 to a cookie write without it).
 */
export async function csrfHeaders(api: ApiClient): Promise<Record<string, string>> {
  const { csrf, header } = await call(api.GET("/v1/auth/web/csrf"));
  return { [header]: csrf };
}
