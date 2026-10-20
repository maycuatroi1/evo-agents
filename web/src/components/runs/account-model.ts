import { queryOptions } from "@tanstack/react-query";

import { type ApiClient, call } from "@/lib/api/client";
import { csrfHeaders } from "@/lib/api/csrf";
import type { components } from "@/lib/api/schema";
import type { ApiSource } from "@/lib/queries";

import { LIVE_REFRESH_MS, type RequestedRuntime, type Run, runKey } from "./queries";

/**
 * The account a member picks by hand for a run (docs/credentials.md, section "Accounts"): one of their own env
 * secrets bound to the project, which the run takes for its variable and keeps, with no failover. Left to the rules,
 * the member's policy for the project picks one when a worker leases the run. A rerun keeps the account picked by
 * hand for the run it reruns, unless the member names another one or asks the rules.
 */
type Schemas = components["schemas"];
export type Account = Schemas["Account"];
export type RerunRequest = Schemas["Rerun"];
export type RunAccount = Schemas["RunAccount"];
export type RunAccountEvent = Schemas["RunAccountEvent"];

/** The choice "auto, by the rules", and, for a rerun, "as the run rerun": no account name has these shapes. */
export const BY_RULES = "";
export const KEEP = "=keep";

export const accountsQuery = (api: ApiSource) =>
  queryOptions({
    queryKey: ["accounts"] as const,
    queryFn: ({ signal }) => call(api().GET("/v1/accounts", { signal })),
    staleTime: 30_000,
  });

/** The accounts run `id` took, by variable, with each usage limit it hit; read again while the run is active. */
export const runAccountsQuery = (api: ApiSource, project: string, id: number, active: boolean) =>
  queryOptions({
    queryKey: [...runKey(project, id), "accounts"] as const,
    queryFn: ({ signal }) =>
      call(api().GET("/v1/projects/{project}/runs/{run_id}/accounts", { params: { path: { project, run_id: id } }, signal })),
    refetchInterval: active ? LIVE_REFRESH_MS : false,
  });

/** The visitor's accounts a run of `project` may take, by name. */
export function projectAccounts(accounts: Account[], project: string): Account[] {
  return accounts.filter((account) => account.projects.includes(project)).sort((a, b) => a.name.localeCompare(b.name));
}

/**
 * The variables each runtime signs in with, for the account a dispatch takes by itself: Claude Code's for any, since a
 * worker tries Claude Code first. None is known for opencode, whose providers each read their own.
 */
const SIGN_IN: Record<RequestedRuntime, readonly string[]> = {
  any: ["CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"],
  "claude-code": ["CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"],
  codex: ["OPENAI_API_KEY", "CODEX_API_KEY"],
  opencode: [],
};

/**
 * The account a dispatch takes when the visitor has not picked one: a ready account of theirs bound to `project`, for a
 * variable `runtime` signs in with, that `worker` may take (one bound to some workers only goes with a run pinned to
 * one of them; null is any worker), the lowest priority first, then the one taken least recently; the rules when none.
 */
export function pickAccount(accounts: Account[], project: string, runtime: RequestedRuntime, worker: string | null): string {
  const variables = SIGN_IN[runtime];
  const ready = projectAccounts(accounts, project).filter(
    (account) =>
      account.state === "ready" &&
      variables.includes(account.env_var) &&
      (account.workers.length === 0 || (worker !== null && account.workers.includes(worker))),
  );
  ready.sort((a, b) => a.priority - b.priority || (a.last_leased_at ?? "").localeCompare(b.last_leased_at ?? "") || a.name.localeCompare(b.name));
  return ready[0]?.name ?? BY_RULES;
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
