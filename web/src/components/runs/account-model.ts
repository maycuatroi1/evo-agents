import { queryOptions } from "@tanstack/react-query";

import { type ApiClient, call } from "@/lib/api/client";
import { csrfHeaders } from "@/lib/api/csrf";
import type { components } from "@/lib/api/schema";
import type { ApiSource } from "@/lib/queries";

import type { Run } from "./queries";

/**
 * The account a member picks by hand for a run (docs/credentials.md, section "Accounts"): one of their own env
 * secrets bound to the project, which the run takes for its variable and keeps, with no failover. Left to the rules,
 * the member's policy for the project picks one when a worker leases the run. A rerun keeps the account picked by
 * hand for the run it reruns, unless the member names another one or asks the rules.
 */
type Schemas = components["schemas"];
export type Account = Schemas["Account"];
export type RerunRequest = Schemas["Rerun"];

/** The choice "auto, by the rules", and, for a rerun, "as the run rerun": no account name has these shapes. */
export const BY_RULES = "";
export const KEEP = "=keep";

export const accountsQuery = (api: ApiSource) =>
  queryOptions({
    queryKey: ["accounts"] as const,
    queryFn: ({ signal }) => call(api().GET("/v1/accounts", { signal })),
    staleTime: 30_000,
  });

/** The visitor's accounts a run of `project` may take, by name. */
export function projectAccounts(accounts: Account[], project: string): Account[] {
  return accounts.filter((account) => account.projects.includes(project)).sort((a, b) => a.name.localeCompare(b.name));
}

/** What a dispatch body says of the choice: nothing for the rules, so a body without an account reads as before. */
export function accountField(choice: string): { account?: string } {
  return choice === BY_RULES || choice === KEEP ? {} : { account: choice };
}

/** The body of a rerun: none to keep the account of the run rerun, else the account named or the rules. */
export function rerunBody(choice: string): RerunRequest | undefined {
  if (choice === KEEP) return undefined;
  return choice === BY_RULES ? { account_by_rule: true } : { account: choice, account_by_rule: false };
}

/** A rerun of run `id` with the session's CSRF header; it answers the new run. */
export async function rerunRun(api: ApiClient, project: string, id: number, choice: string): Promise<Run> {
  const headers = await csrfHeaders(api);
  const params = { path: { project, run_id: id } };
  return call(api.POST("/v1/projects/{project}/runs/{run_id}/rerun", { params, body: rerunBody(choice), headers }));
}
