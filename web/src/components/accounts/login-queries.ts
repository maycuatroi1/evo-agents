import { queryOptions } from "@tanstack/react-query";

import { type ApiClient, call } from "@/lib/api/client";
import { csrfHeaders } from "@/lib/api/csrf";
import type { components } from "@/lib/api/schema";
import type { ApiSource } from "@/lib/queries";

import { accountKeys } from "./queries";

/**
 * What the Add Claude account and Renew token dialog reads and writes, apart from the page's own queries: it loads
 * with the dialog. An account is one of the visitor's env secrets, so the dialog writes it as one, with
 * PUT /v1/secrets/{name}; no answer carries its value.
 */
type Schemas = components["schemas"];
export type SecretWrite = Schemas["SecretWrite"];
export type SecretWritten = Schemas["SecretWritten"];
export type SecretSummary = Pick<Schemas["Secret"], "name" | "kind" | "env_var">;

/** `credentials.SECRET_NAME` and `credentials.MAX_SECRET_BYTES`: an account is a secret, named and sized as one. */
export const SECRET_NAME = /^[a-z0-9][a-z0-9._-]{0,63}$/;
export const MAX_SECRET_NAME_CHARS = 64;
export const MAX_SECRET_BYTES = 16384;

/** The query keys of the Secrets page (`secretKeys.all` there): an account written here is a secret listed there. */
export const SECRETS_PAGE_KEY = ["secrets"] as const;

/**
 * The names of all the visitor's secrets, git ones included, which the accounts list leaves out: a name an account is
 * added under must not be one of theirs that is no Claude account, or the PUT would replace it whole.
 */
export const secretNamesQuery = (api: ApiSource) =>
  queryOptions({
    queryKey: accountKeys.secrets,
    queryFn: async ({ signal }): Promise<SecretSummary[]> => {
      const secrets = await call(api().GET("/v1/secrets", { signal }));
      return secrets.map(({ name, kind, env_var }) => ({ name, kind, env_var }));
    },
  });

/** The workers the visitor may see that are not revoked; the dialog offers those they own. */
export const workersQuery = (api: ApiSource) =>
  queryOptions({
    queryKey: accountKeys.workers,
    queryFn: ({ signal }) => call(api().GET("/v1/workers", { signal })),
  });

/** The worker pools the visitor may see; the dialog offers those they own. */
export const poolsQuery = (api: ApiSource) =>
  queryOptions({
    queryKey: accountKeys.pools,
    queryFn: ({ signal }) => call(api().GET("/v1/worker-pools", { signal })),
  });

// The write, with the session's X-Evo-CSRF header.

/**
 * Add the visitor's Claude account `name`, or give it a new token: PUT /v1/secrets/{name}, kind env, whole. The hub
 * keeps the secret's id on a replace, so its weight, priority, pause, cooldown and runs stay.
 */
export async function putAccountToken(api: ApiClient, name: string, body: SecretWrite): Promise<SecretWritten> {
  const headers = await csrfHeaders(api);
  return call(api.PUT("/v1/secrets/{name}", { params: { path: { name } }, body, headers }));
}
