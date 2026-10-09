import type { Page } from "@playwright/test";

import { call } from "../../src/lib/api/client";

import { type Account, bearerClient, machineToken, uniqueName } from "./hub";
import { claimRun, dispatch, type LiveWorker, liveWorker, REPO, reportState, RUN_PLAN, type Run, sendEvents, type WorkerEvent, workerHeartbeat } from "./runs";

/**
 * Runs for the Monitor's specs, through the real API as a worker daemon would: a plan of independent steps, so one
 * dispatch queues as many runs as a spec needs, and a worker with as many slots, that takes them and starts them, or
 * ends them at once.
 */

/** Steps of the Monitor's plan: enough for the grid's ten tiles and a few more. */
export const MONITOR_STEPS = 14;

export function monitorPlanBody(steps = MONITOR_STEPS) {
  return {
    id: RUN_PLAN,
    title: "Monitor fixtures",
    goal: "Fill the grid.",
    repos: [{ repo: REPO, branch: "main", status: "pending" }],
    steps: Array.from({ length: steps }, (_, index) => ({
      id: index + 1,
      title: `Monitor step ${index + 1}`,
      repo: REPO,
      what: `Do step ${index + 1}.`,
      verify: "pnpm test",
      status: "pending",
    })),
  };
}

/** The Monitor's plan in `project`, which `owner` writes to, with `steps` independent pending steps. */
export async function seedMonitorPlan(owner: Account, project: string, steps = MONITOR_STEPS): Promise<void> {
  const api = bearerClient(await machineToken(owner));
  await call(
    api.PUT("/v1/projects/{project}/plans/{plan_id}", {
      params: { path: { project, plan_id: RUN_PLAN } },
      body: { body: monitorPlanBody(steps), area: "active" },
    }),
  );
}

/** Dispatch `keys` and have a new worker of `owner` take each one; returns the worker and the runs, in key order. */
async function takeRuns(owner: Account, project: string, keys: string[], name: string): Promise<{ live: LiveWorker; runs: Run[] }> {
  const live = await liveWorker(owner, project, name, keys.length);
  const runs = await dispatch(owner, project, keys);
  for (let index = 0; index < runs.length; index += 1) {
    const claimed = await claimRun(live);
    if (!claimed) throw new Error(`the worker claimed nothing after ${index} of ${runs.length} runs`);
  }
  return { live, runs };
}

/** Runs of `keys` the worker took and started: the agent of each is at work. */
export async function runningRuns(owner: Account, project: string, keys: string[], name = uniqueName("monitor")): Promise<{ live: LiveWorker; runs: Run[] }> {
  const taken = await takeRuns(owner, project, keys, name);
  await workerHeartbeat(taken.live, taken.runs.map((run) => run.id));
  for (const run of taken.runs) await reportState(taken.live, run.id, { state: "running", session_id: `3f2a9c1e-0000-4000-8000-${String(run.id).padStart(12, "0")}` });
  return taken;
}

/** Runs of `keys` that failed as soon as a worker of one slot took each: ended runs, whose streams end at once. */
export async function endedRuns(owner: Account, project: string, keys: string[], name = uniqueName("ended")): Promise<Run[]> {
  const live = await liveWorker(owner, project, name);
  const runs = await dispatch(owner, project, keys);
  for (let index = 0; index < runs.length; index += 1) {
    const claimed = await claimRun(live);
    if (!claimed) throw new Error(`the worker claimed nothing after ${index} of ${runs.length} runs`);
    await reportState(live, claimed.id, { state: "failed", error: "stopped for the grid" });
  }
  return runs;
}

/** A tool call the agent ran, with an id of its own so each one is a line of the trace. */
export function command(index: number, text: string): WorkerEvent {
  return { kind: "tool_call", body: { toolCallId: `call-${index}`, title: "Bash", rawInput: { command: text }, status: "completed" } };
}

/** Send `events` as the worker holding the run does. */
export async function trace(live: LiveWorker, runId: number, events: WorkerEvent[]): Promise<number> {
  return sendEvents(live, runId, events);
}

/** The tile of run `id` on the Monitor. */
export function tileOf(page: Page, id: number) {
  return page.locator("#main").locator(`[data-testid="monitor-tile"][data-run-id="${id}"]`);
}

/** The row of run `id` in the Monitor's list. */
export function rowOf(page: Page, id: number) {
  return page.locator("#main").locator(`[data-testid="flight-item"][data-run-id="${id}"]`);
}

/** The Monitor's address for `runs`, each `project:id`. */
export function monitorPath(runs: { project: string; id: number }[]): string {
  return `/monitor?runs=${runs.map((run) => `${run.project}:${run.id}`).join(",")}`;
}

/** The grid as the page lays it out: how many columns and rows its tiles fill, and each row's heights. */
export async function gridLayout(page: Page) {
  return page.evaluate(() => {
    const tiles = Array.from(document.querySelectorAll<HTMLElement>('#main [data-testid="monitor-tile"]'));
    const boxes = tiles.map((tile) => tile.getBoundingClientRect());
    const lefts = [...new Set(boxes.map((box) => Math.round(box.left)))];
    const tops = [...new Set(boxes.map((box) => Math.round(box.top)))];
    const heights = tops.map((top) => boxes.filter((box) => Math.round(box.top) === top).map((box) => box.height));
    const root = document.documentElement;
    const grid = document.querySelector<HTMLElement>('#main [data-testid="monitor-grid"]');
    return {
      cols: lefts.length,
      rows: tops.length,
      heights,
      lowest: Math.max(...boxes.map((box) => box.bottom)),
      viewport: window.innerHeight,
      pageOverflow: root.scrollHeight - root.clientHeight,
      sideways: root.scrollWidth - root.clientWidth,
      gridScrolls: grid ? grid.scrollHeight - grid.clientHeight : 0,
    };
  });
}
