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
export type Role = "reader" | "writer" | "admin";

export const LEVELS = ["public", "internal", "customer", "secret"];

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

/** The hub admin's view of the API, for seeding. Signs in on first use; one token per worker is enough. */
export class HubAdmin {
  private client: Promise<ApiClient> | null = null;

  private api(): Promise<ApiClient> {
    if (DEPLOYED_BASE_URL) throw new Error("seeding needs the local stack; a @deployed spec cannot seed");
    this.client ??= machineToken({ login: ADMIN_LOGIN, id: ADMIN_GITHUB_ID }).then(bearerClient);
    return this.client;
  }

  async registerProject(name: string, overrides: Partial<Registration> = {}): Promise<void> {
    const api = await this.api();
    await call(api.PUT("/v1/projects/{project}", { params: { path: { project: name } }, body: registration(overrides) }));
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
}
