import { queryOptions } from "@tanstack/react-query";

import { type ApiClient, call } from "@/lib/api/client";
import { csrfHeaders } from "@/lib/api/csrf";
import type { components } from "@/lib/api/schema";
import type { ApiSource } from "@/lib/queries";

import { accountKeys } from "./queries";

/**
 * The accounts of an agent (docs/credentials.md, section "Accounts of an agent"): env secrets an agent profile owns,
 * which runs under the agent take on the workers they are bound to. Every member of the agent's project reads them with
 * their figures; only an admin of the project adds, renews, changes, pauses and deletes them. No answer carries a
 * value. Their keys sit under the Accounts page's, so a write anywhere reloads both.
 */
type Schemas = components["schemas"];
export type AgentAccount = Schemas["AgentAccount"];
export type ListedAgentAccount = Schemas["ListedAgentAccount"];
export type AgentAccountWrite = Schemas["AgentAccountWrite"];
export type AgentAccountChange = Schemas["AgentAccountChange"];

/** `agent_accounts.MAX_WORKERS` and `MAX_ACCOUNTS_PER_AGENT`. */
export const MAX_AGENT_ACCOUNT_WORKERS = 100;
export const MAX_ACCOUNTS_PER_AGENT = 50;

export const agentAccountKeys = {
  all: [...accountKeys.all, "agent-accounts"] as const,
  everywhere: [...accountKeys.all, "agent-accounts", "everywhere"] as const,
  of: (project: string, handle: string) => [...accountKeys.all, "agent-accounts", "of", project, handle] as const,
};

/** The live accounts of agent `handle` of `project`, by variable and name. */
export const agentAccountsQuery = (api: ApiSource, project: string, handle: string) =>
  queryOptions({
    queryKey: agentAccountKeys.of(project, handle),
    queryFn: ({ signal }) =>
      call(api().GET("/v1/projects/{project}/agents/{handle}/accounts", { params: { path: { project, handle } }, signal })),
    refetchInterval: 30_000,
  });

/** The live accounts of the agents of every project the visitor holds a grant on, by project, handle and variable. */
export const allAgentAccountsQuery = (api: ApiSource) =>
  queryOptions({
    queryKey: agentAccountKeys.everywhere,
    queryFn: ({ signal }) => call(api().GET("/v1/agent-accounts", { signal })),
    refetchInterval: 30_000,
  });

// Writes, each with the session's X-Evo-CSRF header.

type Target = { project: string; handle: string; name: string };

/** Add the account, or replace the one of that name whole: the value is sealed and never shown again. */
export async function putAgentAccount(api: ApiClient, { project, handle, name }: Target, body: AgentAccountWrite) {
  const headers = await csrfHeaders(api);
  return call(api.PUT("/v1/projects/{project}/agents/{handle}/accounts/{name}", { params: { path: { project, handle, name } }, body, headers }));
}

/** Renew it (`value`), change its weight, priority, workers or end, or pause or resume it. */
export async function changeAgentAccount(api: ApiClient, { project, handle, name }: Target, body: AgentAccountChange) {
  const headers = await csrfHeaders(api);
  return call(api.PATCH("/v1/projects/{project}/agents/{handle}/accounts/{name}", { params: { path: { project, handle, name } }, body, headers }));
}

/** Delete it: its value and bindings go, and the leases of it not given back are revoked. */
export async function deleteAgentAccount(api: ApiClient, { project, handle, name }: Target): Promise<void> {
  const headers = await csrfHeaders(api);
  await call(api.DELETE("/v1/projects/{project}/agents/{handle}/accounts/{name}", { params: { path: { project, handle, name } }, headers }));
}
