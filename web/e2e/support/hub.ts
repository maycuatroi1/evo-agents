import { randomBytes, randomInt } from "node:crypto";

import { createApiClient, type ApiClient, call } from "../../src/lib/api/client";
import type { components } from "../../src/lib/api/schema";

import { ADMIN_LOGIN, API_URL, DEPLOYED_BASE_URL, STACK_URL } from "./env";

/**
 * Seeding through the real API, the way the CLI does it: a GitHub token from the fake, traded for a machine token
 * (POST /v1/auth/github), then project registration and grants as the hub admin. Names are unique per call so
 * tests never see each other's data and can run in parallel.
 */
export type Account = { login: string; id: number };
export type Registration = components["schemas"]["Registration"];
export type Repo = components["schemas"]["Repo"];
export type Role = "reader" | "writer" | "admin";

export const LEVELS = ["public", "internal", "customer", "secret"];

/** The account pool a project the specs register keeps for its agents (HubAdmin.giveAccountPool). */
export const ACCOUNT_POOL = "e2e-accounts";

/** The GitHub id of the stack's hub admin (EVO_HUB_ADMINS lists its login). */
export const ADMIN_GITHUB_ID = 90_000_001;

/** The stack's hub admin, to sign in as in the browser. */
export const ADMIN_ACCOUNT: Account = { login: ADMIN_LOGIN, id: ADMIN_GITHUB_ID };

export function uniqueName(prefix: string): string {
  return `e2e-${prefix}-${randomBytes(4).toString("hex")}`;
}

export function newAccount(prefix = "user"): Account {
  return { login: uniqueName(prefix), id: randomInt(10_000_000, 2_000_000_000) };
}

async function stack<T>(path: string, body: unknown): Promise<T> {
  const response = await fetch(`${STACK_URL}${path}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) throw new Error(`hub_stack ${path}: ${response.status} ${await response.text()}`);
  return (await response.json()) as T;
}

/** A machine token of `account`, as `evo-agents hub login` gets one. */
export async function machineToken(account: Account): Promise<string> {
  const { token } = await stack<{ token: string }>("/github/token", account);
  const api = createApiClient({ baseUrl: API_URL });
  const signed = await call(api.POST("/v1/auth/github", { body: { github_token: token, host: "playwright" } }));
  return signed.token;
}

/**
 * A machine token of `account` that nobody used for `days` days: last used then, expiring 90 days after, as the hub
 * leaves an idle token (hub_stack POST /tokens/idle); its id. Using it would move both forward, so it is not returned.
 */
export async function idleMachineToken(account: Account, days: number): Promise<number> {
  const { token } = await stack<{ token: string }>("/github/token", account);
  const api = createApiClient({ baseUrl: API_URL });
  const signed = await call(api.POST("/v1/auth/github", { body: { github_token: token, host: "playwright" } }));
  await stack("/tokens/idle", { token_id: signed.token_id, days });
  return signed.token_id;
}

export function bearerClient(token: string): ApiClient {
  return createApiClient({ baseUrl: API_URL, headers: { authorization: `Bearer ${token}` } });
}

export function registration(overrides: Partial<Registration> = {}): Registration {
  return {
    levels: LEVELS,
    locations: ["any", "domestic-only"],
    default_label: { level: "internal", integrity: "U" },
    sinks: [
      { id: "hub", kind: "hub", clearance: { level: "internal" } },
      { id: "claude-code", kind: "agent-session", clearance: { level: "customer", location: "domestic-only" } },
    ],
    repos: [{ name: "api", origin: "https://github.com/example-org/api", default_branch: "main", path: "api" }],
    harness: { name: "example-harness", workspace: "~/work", path: "example-harness" },
    ...overrides,
  };
}

/** The hub admin's Claude account in the account pools, made on first use (HubAdmin.poolAccount). */
let adminPoolAccount: Promise<string> | null = null;

/** The hub admin's view of the API, for seeding. Signs in on first use; one token per worker is enough. */
export class HubAdmin {
  private client: Promise<ApiClient> | null = null;

  private api(): Promise<ApiClient> {
    if (DEPLOYED_BASE_URL) throw new Error("seeding needs the local stack; a @deployed spec cannot seed");
    this.client ??= machineToken({ login: ADMIN_LOGIN, id: ADMIN_GITHUB_ID }).then(bearerClient);
    return this.client;
  }

  /**
   * Register project `name`. Its default agent (handle `name`) gets the project's account pool ACCOUNT_POOL, as an
   * admin attaches one: the hub refuses to dispatch a run of an agent with no account pool (plan agent-dedicated-pools).
   */
  async registerProject(name: string, overrides: Partial<Registration> = {}): Promise<void> {
    const api = await this.api();
    await call(api.PUT("/v1/projects/{project}", { params: { path: { project: name } }, body: registration(overrides) }));
    await this.giveAccountPool(name);
  }

  /**
   * List `repo` among the project's repos, in place of one of the same name, keeping the rest of what the project
   * registered: the PUT of `evo-agents hub project register` once its harness lists the repo.
   */
  async addRepo(project: string, repo: Repo): Promise<void> {
    const api = await this.api();
    const held = await call(api.GET("/v1/projects/{project}", { params: { path: { project } } }));
    if (!held.harness) throw new Error(`project ${project} was registered without its harness paths`);
    const body: Registration = {
      levels: held.levels,
      locations: held.locations,
      default_label: held.default_label as Registration["default_label"],
      sinks: held.sinks,
      repos: [...held.repos.filter((listed) => listed.name !== repo.name), repo],
      harness: held.harness,
    };
    await call(api.PUT("/v1/projects/{project}", { params: { path: { project } }, body }));
  }

  /**
   * Agent `handle` of `project` (its default agent unless named) with the project's account pool ACCOUNT_POOL
   * attached, as `evo-agents hub agent account-pool HANDLE POOL` attaches one. The pool is made on first use and holds
   * the hub admin's Claude account (poolAccount): a run of an agent with no account pool is refused at dispatch, and a
   * node of the agent's pool claims a run only while its account pool has an account the run can use. The tests'
   * `give_account_pool` (tests/hub/account_pool_setup.py) does the same with rows.
   */
  async giveAccountPool(project: string, handle: string = project): Promise<void> {
    const api = await this.api();
    const pool = { project, pool: ACCOUNT_POOL };
    const held = await api.GET("/v1/projects/{project}/account-pools/{pool}", { params: { path: pool } });
    if (held.response.status === 404) {
      const account = await this.poolAccount();
      await call(api.POST("/v1/projects/{project}/account-pools", { params: { path: { project } }, body: { name: ACCOUNT_POOL } }));
      await call(api.PUT("/v1/projects/{project}/account-pools/{pool}/accounts", { params: { path: pool }, body: { name: account } }));
    } else if (!held.response.ok) {
      throw new Error(`GET the account pool ${ACCOUNT_POOL} of ${project}: ${held.response.status}`);
    }
    await call(api.PUT("/v1/projects/{project}/agents/{handle}/account-pool", { params: { path: { project, handle } }, body: { account_pool: ACCOUNT_POOL } }));
  }

  /**
   * The name of the hub admin's Claude account (CLAUDE_CODE_OAUTH_TOKEN, a made-up value) the account pools hold, made
   * once per test worker. A secret names the projects whose runs get it, with the writer role on each, and the hub admin
   * holds no grant on the projects the specs register: it names a project of its own, which no spec reads. An account
   * pool takes the account whatever projects it names.
   */
  private poolAccount(): Promise<string> {
    adminPoolAccount ??= this.makePoolAccount().catch((error: unknown) => {
      adminPoolAccount = null;
      throw error;
    });
    return adminPoolAccount;
  }

  private async makePoolAccount(): Promise<string> {
    const api = await this.api();
    const home = uniqueName("pools");
    await call(api.PUT("/v1/projects/{project}", { params: { path: { project: home } }, body: registration() }));
    await this.grant(home, ADMIN_LOGIN, "writer", "internal");
    const name = uniqueName("claude");
    const value = `e2e-pool-${randomBytes(12).toString("hex")}`;
    await call(api.PUT("/v1/secrets/{name}", { params: { path: { name } }, body: { kind: "env", env_var: "CLAUDE_CODE_OAUTH_TOKEN", projects: [home], value } }));
    return name;
  }

  async grant(project: string, login: string, role: Role, maxLevel: string): Promise<void> {
    const api = await this.api();
    await call(
      api.PUT("/v1/admin/projects/{project}/grants/{login}", {
        params: { path: { project, login } },
        body: { role, max_level: maxLevel },
      }),
    );
  }

  /** An invite link made as the hub admin: its row, with the token shown once and the link when the hub has a URL. */
  async invite(body: components["schemas"]["InviteRequest"]): Promise<components["schemas"]["InviteCreated"]> {
    const api = await this.api();
    return call(api.POST("/v1/admin/invites", { body }));
  }

  /** The retention of built graphs on one project, keeping the artifacts of its `keep` newest graphs. */
  async pruneKg(project: string, keep: number) {
    const api = await this.api();
    return call(api.POST("/v1/admin/kg/prune", { body: { project, keep, dry_run: false } }));
  }
}
