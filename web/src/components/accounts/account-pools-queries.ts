import { queryOptions } from "@tanstack/react-query";

import { type ApiClient, call } from "@/lib/api/client";
import { csrfHeaders } from "@/lib/api/csrf";
import type { components } from "@/lib/api/schema";
import type { ApiSource } from "@/lib/queries";

import { accountKeys } from "./queries";

/**
 * The account pools of a project (docs/credentials.md, section "Account pools"): named sets of Claude, provider and
 * env accounts that an admin of the project attaches to its agents. An agent has one; a run of an agent in pool mode
 * leases from it alone. Only an admin of the project or of the hub reads or changes them.
 */
export type AccountPool = components["schemas"]["AccountPool"];

export const accountPoolKeys = {
  all: [...accountKeys.all, "account-pools"] as const,
  of: (project: string) => [...accountKeys.all, "account-pools", project] as const,
};

export const accountPoolsQuery = (api: ApiSource, project: string) =>
  queryOptions({
    queryKey: accountPoolKeys.of(project),
    queryFn: ({ signal }) => call(api().GET("/v1/projects/{project}/account-pools", { params: { path: { project } }, signal })),
    refetchInterval: 30_000,
  });

export async function createAccountPool(api: ApiClient, project: string, name: string) {
  const headers = await csrfHeaders(api);
  return call(api.POST("/v1/projects/{project}/account-pools", { params: { path: { project } }, body: { name }, headers }));
}

export async function deleteAccountPool(api: ApiClient, project: string, pool: string): Promise<void> {
  const headers = await csrfHeaders(api);
  await call(api.DELETE("/v1/projects/{project}/account-pools/{pool}", { params: { path: { project, pool } }, headers }));
}

/** Add the account `name` of the visitor, or of the agent `agent` of the project, to the pool. */
export async function addPoolAccount(api: ApiClient, project: string, pool: string, name: string, agent: string | null) {
  const headers = await csrfHeaders(api);
  return call(api.PUT("/v1/projects/{project}/account-pools/{pool}/accounts", { params: { path: { project, pool } }, body: { name, agent }, headers }));
}

export async function removePoolAccount(api: ApiClient, project: string, pool: string, secretId: number) {
  const headers = await csrfHeaders(api);
  return call(api.DELETE("/v1/projects/{project}/account-pools/{pool}/accounts/{secret_id}", { params: { path: { project, pool, secret_id: secretId } }, headers }));
}

/** Attach the pool `pool` to agent `handle`, or none (null) to detach. */
export async function attachAccountPool(api: ApiClient, project: string, handle: string, pool: string | null) {
  const headers = await csrfHeaders(api);
  return call(api.PUT("/v1/projects/{project}/agents/{handle}/account-pool", { params: { path: { project, handle } }, body: { account_pool: pool }, headers }));
}
