import type { Page } from "@playwright/test";

import { call } from "../../src/lib/api/client";
import type { components } from "../../src/lib/api/schema";

import { API_URL, STACK_URL } from "./env";
import { type Account, machineToken } from "./hub";
import { apiOf } from "./memories";

/**
 * Workers for the workers pages, made the ways a machine makes one: `evo-agents worker register` on a machine signed
 * in with `evo-agents hub login` (POST /v1/workers with its machine token), or `evo-agents worker join` with a pairing
 * code (POST /v1/worker/join, public, with the protocol header). This release of the API has no heartbeat route yet,
 * so the stack records a heartbeat the way the daemon's would (POST /workers/heartbeat on its control port).
 */
export type Worker = components["schemas"]["Worker"];
export type WorkerCredential = components["schemas"]["WorkerCredential"];

export const PROTOCOL_HEADER = { "X-Evo-Worker-Protocol": "1" };

export const HOST = { hostname: "e2e-host.local", os: "macOS 15", arch: "arm64", agent_version: "0.4.0" };

/** What a daemon's heartbeat reports, in the shape the pages read. */
export const RUNTIMES = {
  "claude-code": { available: true, version: "2.1.289", auth: "signed in" },
  opencode: { available: true, version: "1.18.34" },
  codex: { available: false, reason: "not found on PATH" },
};
export const CHECKOUTS = {
  api: { path: "~/github/api", branch: "main" },
  "example-harness": { path: "~/github/example-harness", branch: "main" },
};

type Registration = { name: string; projects: string[]; slots?: number; labels?: string[]; terminal?: boolean };

/** A worker of `account`, registered from a signed-in machine. */
export async function registerWorker(account: Account, input: Registration): Promise<Worker> {
  const api = await apiOf(account);
  const credential = await call(
    api.POST("/v1/workers", {
      body: {
        ...HOST,
        hostname: `${input.name}.local`,
        name: input.name,
        projects: input.projects,
        slots: input.slots ?? 1,
        labels: input.labels ?? [],
        allow_web_terminal: input.terminal ?? false,
      },
    }),
  );
  return credential.worker;
}

/** A heartbeat of the worker `seconds_ago` seconds ago, reporting `report` when given. */
export async function heartbeat(
  workerId: number,
  report: { runtimes?: object; checkouts?: object; secondsAgo?: number } = {},
): Promise<void> {
  const response = await fetch(`${STACK_URL}/workers/heartbeat`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({
      worker_id: workerId,
      runtimes: report.runtimes,
      checkouts: report.checkouts,
      seconds_ago: report.secondsAgo ?? 0,
    }),
  });
  if (!response.ok) throw new Error(`hub_stack /workers/heartbeat: ${response.status} ${await response.text()}`);
}

/** What the daemon does with a pairing code: trade it for its worker token. */
export async function joinWithCode(code: string, hostname = "joined-host.local"): Promise<Response> {
  return fetch(`${API_URL}/v1/worker/join`, {
    method: "POST",
    headers: { "content-type": "application/json", ...PROTOCOL_HEADER },
    body: JSON.stringify({ ...HOST, hostname, code }),
  });
}

/** The workers `account` sees through the API, revoked ones included. */
export async function workersOf(account: Account): Promise<Worker[]> {
  const token = await machineToken(account);
  const response = await fetch(`${API_URL}/v1/workers?revoked=true`, { headers: { authorization: `Bearer ${token}` } });
  if (!response.ok) throw new Error(`GET /v1/workers: ${response.status}`);
  return (await response.json()) as Worker[];
}

/** The session's CSRF header, for a cookie write made with page.request. */
export async function csrfHeader(page: Page): Promise<Record<string, string>> {
  const answer = await page.request.get("/v1/auth/web/csrf");
  const { csrf, header } = (await answer.json()) as { csrf: string; header: string };
  return { [header]: csrf };
}
