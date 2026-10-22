import { queryOptions } from "@tanstack/react-query";

import { type ApiClient, call } from "@/lib/api/client";
import { csrfHeaders } from "@/lib/api/csrf";
import type { components } from "@/lib/api/schema";
import type { ApiSource } from "@/lib/queries";

/**
 * What the Accounts page reads and writes (docs/credentials.md, section "Accounts"). An account is one of the visitor's
 * own env secrets, seen as one a run may take among several for the same variable; a policy is the visitor's for one
 * project. A member sees and changes only their own, a hub admin included; no answer carries a value.
 */
type Schemas = components["schemas"];
export type Account = Schemas["Account"];
export type AccountState = Account["state"];
/** What an account of the visitor's and an account of an agent have in common: what the shared cells read. */
export type AccountFigures = Omit<Account, "projects" | "auth" | "shares" | "cost_by_principal"> & Partial<Pick<Account, "auth">>;
export type AccountUsage = NonNullable<Account["last_usage"]>;
export type AccountChange = Schemas["AccountChange"];
export type AccountPolicy = Schemas["AccountPolicy"];
export type AccountPolicyWrite = Schemas["AccountPolicyWrite"];
export type Strategy = AccountPolicy["strategy"];

/** `accounts.STRATEGIES` of the API, in its order. */
export const STRATEGIES = ["round-robin", "weighted-round-robin", "fill-first"] as const satisfies readonly Strategy[];
/** `accounts.MAX_WEIGHT`, `MAX_PRIORITY`, `MIN_COOLDOWN_SECONDS`, `MAX_COOLDOWN_SECONDS` and `MAX_FAILOVERS`. */
export const MAX_WEIGHT = 1000;
export const MAX_PRIORITY = 1000;
export const MIN_COOLDOWN_SECONDS = 60;
export const MAX_COOLDOWN_SECONDS = 7 * 24 * 3600;
export const MAX_FAILOVERS = 20;
/** `accounts.DEFAULT_POLICY`: what holds for a project the visitor set no policy for. */
export const DEFAULT_POLICY: AccountPolicyWrite = {
  strategy: "round-robin",
  cooldown_seconds: 3600,
  failover: true,
  max_failovers: 3,
};

/** The variable a Claude account sets: the token of `claude setup-token` (docs/credentials.md, section "Accounts"). */
export const CLAUDE_TOKEN_VAR = "CLAUDE_CODE_OAUTH_TOKEN";

export const accountKeys = {
  all: ["accounts"] as const,
  list: ["accounts", "list"] as const,
  policies: ["accounts", "policies"] as const,
  secrets: ["accounts", "secrets"] as const,
  workers: ["accounts", "workers"] as const,
};

/** The visitor's own accounts with their figures, by variable and name, without their values. */
export const accountListQuery = (api: ApiSource) =>
  queryOptions({
    queryKey: accountKeys.list,
    queryFn: ({ signal }) => call(api().GET("/v1/accounts", { signal })),
    refetchInterval: 30_000,
  });

/** The policies the visitor set, by project; a project without one has DEFAULT_POLICY. */
export const policiesQuery = (api: ApiSource) =>
  queryOptions({
    queryKey: accountKeys.policies,
    queryFn: ({ signal }) => call(api().GET("/v1/account-policies", { signal })),
  });

// Writes, each with the session's X-Evo-CSRF header.

/** Change the weight or priority of the visitor's account `name`, or pause or resume it. */
export async function changeAccount(api: ApiClient, name: string, body: AccountChange): Promise<Account> {
  const headers = await csrfHeaders(api);
  return call(api.PATCH("/v1/accounts/{name}", { params: { path: { name } }, body, headers }));
}

/** Set the visitor's policy for `project` whole. */
export async function putPolicy(api: ApiClient, project: string, body: AccountPolicyWrite): Promise<AccountPolicy> {
  const headers = await csrfHeaders(api);
  return call(api.PUT("/v1/account-policies/{project}", { params: { path: { project } }, body, headers }));
}

/** Drop the visitor's policy for `project`: the defaults hold again. */
export async function deletePolicy(api: ApiClient, project: string): Promise<void> {
  const headers = await csrfHeaders(api);
  await call(api.DELETE("/v1/account-policies/{project}", { params: { path: { project } }, headers }));
}
