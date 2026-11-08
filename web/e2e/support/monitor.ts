import { call } from "../../src/lib/api/client";

import { type Account, bearerClient, machineToken, uniqueName } from "./hub";
import { claimRun, dispatch, type LiveWorker, liveWorker, REPO, reportState, RUN_PLAN, type Run, workerHeartbeat } from "./runs";

/**
 * Runs at work for the specs that need some (the org chart, autonomy), through the real API as a worker daemon would:
 * a plan of independent steps, so one dispatch queues as many runs as a spec needs, and a worker with as many slots,
 * that takes them and starts them.
 */

/** Steps of the plan: more runs than any spec starts. */
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

/** The plan in `project`, which `owner` writes to, with `steps` independent pending steps. */
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
