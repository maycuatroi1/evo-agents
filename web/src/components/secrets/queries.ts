import { queryOptions } from "@tanstack/react-query";
import type { Route } from "next";

import { type ApiClient, call } from "@/lib/api/client";
import { csrfHeaders } from "@/lib/api/csrf";
import type { components } from "@/lib/api/schema";
import type { ApiSource } from "@/lib/queries";

/**
 * What the Secrets page reads and writes (docs/credentials.md). A member sees and changes only their own secrets, a hub
 * admin included; no answer of the hub ever carries a value. A PUT writes the whole secret, value included, so the page
 * sends a value with every save and keeps none of it afterwards.
 */
type Schemas = components["schemas"];
export type Secret = Schemas["Secret"];
export type SecretWrite = Schemas["SecretWrite"];
export type SecretWritten = Schemas["SecretWritten"];
export type SecretKind = Secret["kind"];

/** `credentials.SECRET_KINDS` of the API, in its order. */
export const SECRET_KINDS = ["env", "git"] as const satisfies readonly SecretKind[];
/** `credentials.SECRET_NAME`: unique among the owner's secrets. */
export const SECRET_NAME = /^[a-z0-9][a-z0-9._-]{0,63}$/;
export const MAX_SECRET_NAME_CHARS = 64;
/** `credentials.ENV_NAME`, `DENIED_ENV` and `DENIED_ENV_PREFIXES`: what an env secret may set. */
export const ENV_NAME = /^[A-Z_][A-Z0-9_]*$/;
export const DENIED_ENV = ["PATH", "HOME", "SHELL", "USER", "TMPDIR", "SSH_AUTH_SOCK"] as const;
export const DENIED_ENV_PREFIXES = ["EVO_", "GIT_", "LD_", "DYLD_", "PYTHON"] as const;
/** `secrets.MAX_ENV_VAR_CHARS`, `MAX_URL_PREFIX_CHARS` and `MAX_USERNAME_CHARS`. */
export const MAX_ENV_VAR_CHARS = 200;
export const MAX_URL_PREFIX_CHARS = 2000;
export const MAX_USERNAME_CHARS = 200;
/** `credentials.MAX_SECRET_BYTES`: a value is 1 to 16384 bytes of UTF-8. */
export const MAX_SECRET_BYTES = 16384;
/** `credentials.MAX_SECRETS_PER_OWNER`. */
export const MAX_SECRETS_PER_OWNER = 200;
/** `credentials.DEFAULT_GIT_USERNAME`: what git sends with the value when no username is given. */
export const DEFAULT_GIT_USERNAME = "oauth2";

export const secretKeys = {
  all: ["secrets"] as const,
  list: ["secrets", "list"] as const,
};

/** The visitor's own secrets, by name, without their values. */
export const secretsQuery = (api: ApiSource) =>
  queryOptions({
    queryKey: secretKeys.list,
    queryFn: ({ signal }) => call(api().GET("/v1/secrets", { signal })),
  });

// Writes, each with the session's X-Evo-CSRF header.

/** Create the visitor's secret `name`, or replace it whole; `created` in the answer says which. */
export async function putSecret(api: ApiClient, name: string, body: SecretWrite): Promise<SecretWritten> {
  const headers = await csrfHeaders(api);
  return call(api.PUT("/v1/secrets/{name}", { params: { path: { name } }, body, headers }));
}

/** Delete the visitor's secret `name`: its value and bindings go, and the leases of it still out are revoked. */
export async function deleteSecret(api: ApiClient, name: string): Promise<void> {
  const headers = await csrfHeaders(api);
  await call(api.DELETE("/v1/secrets/{name}", { params: { path: { name } }, headers }));
}

export const SECRETS_HREF = "/secrets" as Route;
