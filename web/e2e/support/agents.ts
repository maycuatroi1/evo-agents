import { call } from "../../src/lib/api/client";
import type { components } from "../../src/lib/api/schema";

import { ACCOUNT_POOL, type Account } from "./hub";
import { apiOf } from "./memories";

/**
 * A project's agent profiles through the real API, as `evo-agents hub agent create|show` does with the admin's machine
 * token: the specs seed agents this way and read back what the web saved.
 */
export type Agent = components["schemas"]["Agent"];
export type AgentCreate = components["schemas"]["AgentCreate"];

/** A 1 by 1 PNG, the smallest image the hub takes as an avatar. */
export const PIXEL_PNG = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==",
  "base64",
);

/** An agent as a spec writes one: the description and instructions empty unless given, as the API defaults them. */
export type AgentInput = Omit<AgentCreate, "description" | "instructions"> & Partial<Pick<AgentCreate, "description" | "instructions">>;

/**
 * Agent `input` made by `admin` (an admin of `project`), with the project's account pool ACCOUNT_POOL attached: the hub
 * refuses to dispatch a run of an agent with no account pool (plan agent-dedicated-pools). Its runs go to a node of its
 * own pool, `agent-<handle>` (liveWorker's `agent`).
 */
export async function createAgentByApi(admin: Account, project: string, input: AgentInput): Promise<Agent> {
  const api = await apiOf(admin);
  const body: AgentCreate = { description: "", instructions: "", ...input };
  const agent = await call(api.POST("/v1/projects/{project}/agents", { params: { path: { project } }, body }));
  return call(
    api.PUT("/v1/projects/{project}/agents/{handle}/account-pool", {
      params: { path: { project, handle: agent.handle } },
      body: { account_pool: ACCOUNT_POOL },
    }),
  );
}

export async function agentOf(account: Account, project: string, handle: string): Promise<Agent> {
  const api = await apiOf(account);
  return call(api.GET("/v1/projects/{project}/agents/{handle}", { params: { path: { project, handle } } }));
}

export async function putAvatarByApi(admin: Account, project: string, handle: string, image: Buffer = PIXEL_PNG): Promise<Agent> {
  const api = await apiOf(admin);
  return call(
    api.PUT("/v1/projects/{project}/agents/{handle}/avatar", {
      params: { path: { project, handle } },
      body: { data: image.toString("base64") },
    }),
  );
}

export function agentsPath(project: string): string {
  return `/p/${project}/agents`;
}

export function agentPath(project: string, handle: string): string {
  return `${agentsPath(project)}/${handle}`;
}

export type AgentAccount = components["schemas"]["AgentAccount"];
export type AgentAccountWrite = components["schemas"]["AgentAccountWrite"];

/** Add agent `handle`'s account `name`, or replace it whole, as an admin of `project` does. */
export async function putAgentAccountByApi(admin: Account, project: string, handle: string, name: string, body: AgentAccountWrite): Promise<AgentAccount> {
  const api = await apiOf(admin);
  return call(api.PUT("/v1/projects/{project}/agents/{handle}/accounts/{name}", { params: { path: { project, handle, name } }, body }));
}

/** The live accounts of agent `handle` as `account` reads them. */
export async function agentAccountsOf(account: Account, project: string, handle: string): Promise<AgentAccount[]> {
  const api = await apiOf(account);
  return call(api.GET("/v1/projects/{project}/agents/{handle}/accounts", { params: { path: { project, handle } } }));
}

/**
 * Account pool `pool` of `project` made by `admin` (an admin of the project) with `admin`'s own accounts `accounts` in
 * it, in that order, and attached to agent `handle` (the project's default agent unless named), as an admin does with
 * `evo-agents hub account-pool create|add` and `evo-agents hub agent account-pool`: a run of the agent leases the
 * accounts of its account pool alone, never its dispatcher's (plan agent-dedicated-pools).
 */
export async function poolAccountsOf(admin: Account, project: string, accounts: string[], { handle = project, pool = "mine" }: { handle?: string; pool?: string } = {}): Promise<void> {
  const api = await apiOf(admin);
  await call(api.POST("/v1/projects/{project}/account-pools", { params: { path: { project } }, body: { name: pool } }));
  for (const name of accounts) {
    await call(api.PUT("/v1/projects/{project}/account-pools/{pool}/accounts", { params: { path: { project, pool } }, body: { name } }));
  }
  await call(api.PUT("/v1/projects/{project}/agents/{handle}/account-pool", { params: { path: { project, handle } }, body: { account_pool: pool } }));
}
