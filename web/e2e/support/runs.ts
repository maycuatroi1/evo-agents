import type { Page } from "@playwright/test";

import { type ApiClient, call } from "../../src/lib/api/client";
import type { components } from "../../src/lib/api/schema";

import { API_URL } from "./env";
import { type Account, bearerClient, machineToken } from "./hub";
import { HOST, PROTOCOL_HEADER } from "./workers";

/**
 * Plans with steps to run, workers that can take them, and the worker's side of the queue, all through the real API:
 * a plan pushed as a writer (`evo-agents hub plan import`), a worker registered from a signed-in machine
 * (`evo-agents worker register`), and the daemon's heartbeat and claim with its worker token (docs/workers.md).
 */
export type Run = components["schemas"]["Run"];
export type Worker = components["schemas"]["Worker"];

/** The plan the runs specs dispatch from. */
export const RUN_PLAN = "rollout";
/** Step 1 is done, 2 and 4 are ready, 3 waits for 2, and 5 is in progress. */
export const READY_STEPS = ["2", "4"];
export const WAITING_STEP = "3";
export const DONE_STEP = "1";
export const STEP_TITLES: Record<string, string> = {
  "1": "Run model and protocol",
  "2": "Queue with dispatch and claim",
  "3": "Worker daemon",
  "4": "Runs page on the web",
  "5": "Release notes",
};

/** The repo the plan's steps run in: the stack's projects register one repo, api. */
export const REPO = "api";

export function runPlanBody(id = RUN_PLAN) {
  const step = (key: number, extra: Record<string, unknown>) => ({
    id: key,
    title: STEP_TITLES[String(key)],
    repo: REPO,
    what: `Do step ${key}.`,
    verify: "pnpm test",
    ...extra,
  });
  return {
    id,
    title: "Worker fleet rollout",
    goal: "Run plan steps on workers.",
    repos: [{ repo: REPO, branch: "main", status: "pending" }],
    steps: [
      step(1, { status: "done", evidence: "api@abc1234: 40 tests pass" }),
      step(2, { status: "pending", depends_on: [1] }),
      step(3, { status: "pending", depends_on: [2] }),
      step(4, { status: "pending" }),
      step(5, { status: "in_progress" }),
    ],
  };
}

/** Push the runs plan into `project` as `writer`, who holds the writer role there. */
export async function seedRunPlan(writer: Account, project: string, id = RUN_PLAN): Promise<void> {
  const api = bearerClient(await machineToken(writer));
  await call(
    api.PUT("/v1/projects/{project}/plans/{plan_id}", {
      params: { path: { project, plan_id: id } },
      body: { body: runPlanBody(id), area: "active" },
    }),
  );
}

export type LiveWorker = { worker: Worker; token: string; project: string };

/**
 * A worker of `account` serving `project`, registered from a signed-in machine, after a first heartbeat that reports
 * Claude Code and a checkout of the plan's repo: a dispatch to `account` matches it.
 */
export async function liveWorker(account: Account, project: string, name: string, slots = 1): Promise<LiveWorker> {
  const api = bearerClient(await machineToken(account));
  const credential = await call(
    api.POST("/v1/workers", {
      body: {
        ...HOST,
        hostname: `${name}.local`,
        name,
        projects: [project],
        slots,
        labels: [],
        allow_web_terminal: false,
      },
    }),
  );
  const live = { worker: credential.worker, token: credential.token, project };
  await workerHeartbeat(live);
  return live;
}

async function workerCall(live: LiveWorker, path: string, body: unknown): Promise<unknown> {
  const response = await fetch(`${API_URL}${path}`, {
    method: "POST",
    headers: { "content-type": "application/json", authorization: `Bearer ${live.token}`, ...PROTOCOL_HEADER },
    body: JSON.stringify(body),
  });
  if (!response.ok) throw new Error(`POST ${path}: ${response.status} ${await response.text()}`);
  return response.json();
}

/** The daemon's heartbeat: Claude Code available, a checkout of the plan's repo, the runs it holds. */
export async function workerHeartbeat(live: LiveWorker, runs: number[] = []): Promise<void> {
  await workerCall(live, "/v1/worker/heartbeat", {
    runtimes: { "claude-code": { available: true, version: "2.1.289" }, codex: { available: false, reason: "not found on PATH" } },
    checkouts: { [`${live.project}/${REPO}`]: { path: `~/github/${REPO}`, branch: "main" } },
    free_slots: Math.max(live.worker.slots - runs.length, 0),
    runs,
  });
}

/** The daemon's claim, answered at once: the run it leased, or null. */
export async function claimRun(live: LiveWorker): Promise<{ id: number } | null> {
  const answer = (await workerCall(live, "/v1/worker/claim", { wait_s: 0 })) as { run: { id: number } | null };
  return answer.run;
}

/** The daemon reporting a move of a run it holds, such as {state: "failed", error}. */
export async function reportState(live: LiveWorker, runId: number, report: Record<string, unknown>): Promise<Run> {
  return (await workerCall(live, `/v1/worker/runs/${runId}/state`, report)) as Run;
}

/** Dispatch through the API as `account`, the way `evo-agents hub run` will. */
export async function dispatch(account: Account, project: string, steps: string[], extra: Record<string, unknown> = {}): Promise<Run[]> {
  const api: ApiClient = bearerClient(await machineToken(account));
  return call(
    api.POST("/v1/projects/{project}/runs", {
      params: { path: { project } },
      body: { plan_id: RUN_PLAN, steps, runtime: "any", mode: "headless", approval: "review", timeout_min: 60, ...extra },
    }),
  );
}

/** The project's runs as `account` reads them through the API, newest first. */
export async function runsOf(account: Account, project: string): Promise<Run[]> {
  const api = bearerClient(await machineToken(account));
  const list = await call(api.GET("/v1/projects/{project}/runs", { params: { path: { project }, query: { limit: 200 } } }));
  return list.runs;
}

/** The row of run `id` in a runs table. */
export function runRow(page: Page, id: number, table = "runs-table") {
  return page.locator("#main").getByTestId(table).locator("tbody tr").filter({ has: page.locator(`[data-run-id="${id}"]`) });
}
