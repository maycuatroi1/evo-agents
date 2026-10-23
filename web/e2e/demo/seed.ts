import { call } from "../../src/lib/api/client";
import { STACK_URL } from "../support/env";
import { bearerClient, HubAdmin, machineToken, type Registration } from "../support/hub";
import {
  askDecision,
  claimRun,
  dispatch,
  dispatchPlan,
  liveWorker,
  type LiveWorker,
  MODELS,
  reportState,
  reportStep,
  type Run,
  sendEvents,
  sendNotice,
  workerHeartbeat,
} from "../support/runs";

import {
  ago,
  API,
  CHECKOUT_BODY,
  CHECKOUT_PLAN,
  DECISION,
  DECISION_AT,
  MACHINES,
  MAYA,
  NOTICE,
  NOTICE_AT,
  NOW,
  type Person,
  PRIYA,
  PROJECT,
  REGISTRATION,
  SEARCH_BODY,
  SEARCH_PLAN,
  TOMAS,
} from "./story";
import { mayaTrace, planRunTrace, priyaTrace, TOMAS_SESSION, tomasTrace } from "./trace";

/**
 * The demo seeded into the empty hub of `pnpm screenshots`, through the API as the CLI and the worker daemon send it:
 * the project and its grants, both plans, the knowledge graph (hub_stack POST /demo/kg), the three machines, three runs
 * of Checkout v2 and the plan run of Product search waiting for Maya's answer. Then the times the hub stamped while
 * this ran are set to the story's (POST /demo/retime). Answers what the images open.
 */
export type Demo = { workers: number[]; runs: number[]; tomasRun: number; planRun: number };

async function stack<T>(path: string, body: unknown): Promise<T> {
  const response = await fetch(`${STACK_URL}${path}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) throw new Error(`hub_stack ${path}: ${response.status} ${await response.text()}`);
  return (await response.json()) as T;
}

/** The workers and runs keep looking alive: call before each page, since nothing sends their heartbeats. */
export function keepAlive(demo: Demo): Promise<unknown> {
  return stack("/demo/keepalive", { workers: demo.workers, runs: demo.runs });
}

type Machine = { owner: Person; name: string; slots: number; repos: string[] };

function machine(spec: Machine): Promise<LiveWorker> {
  return liveWorker(spec.owner, PROJECT, spec.name, spec.slots, { report: { repos: spec.repos, models: MODELS } });
}

/** The worker of `live` claims the run just queued for it, and its agent starts. */
async function start(live: LiveWorker, run: Run, holding: number[], session: string, repos: string[]): Promise<void> {
  const claimed = await claimRun(live);
  if (claimed?.id !== run.id) throw new Error(`${live.worker.name} claimed ${claimed?.id ?? "nothing"}, not run #${run.id}`);
  await workerHeartbeat(live, [...holding, run.id], { repos, models: MODELS });
  await reportState(live, run.id, { state: "running", session_id: session });
}

/** One step of Checkout v2 run by `spec`'s owner on that machine. */
async function stepRun(live: LiveWorker, spec: Machine, step: string, model: string, session: string, holding: number[] = []): Promise<Run> {
  const [run] = await dispatch(spec.owner, PROJECT, [step], {
    plan_id: CHECKOUT_PLAN,
    worker_id: live.worker.id,
    runtime: "claude-code",
    model,
    approval: "review",
    timeout_min: 90,
  });
  await start(live, run, holding, session, spec.repos);
  return run;
}

/** The times of a run: queued, leased and started `minutes` ago, its agent time counted up to NOW unless it waits. */
function runTimes(id: number, minutes: number, extra: Record<string, unknown> = {}) {
  return {
    table: "runs",
    where: { id },
    set: {
      queued_at: ago(minutes + 0.6),
      leased_at: ago(minutes + 0.3),
      started_at: ago(minutes),
      run_seconds: Math.round(minutes * 60),
      counted_at: ago(0),
      ...extra,
    },
  };
}

export async function seedDemo(): Promise<Demo> {
  const admin = new HubAdmin();
  await admin.registerProject(PROJECT, REGISTRATION as Registration);
  await admin.grant(PROJECT, MAYA.login, "admin", "customer");
  await admin.grant(PROJECT, TOMAS.login, "writer", "internal");
  await admin.grant(PROJECT, PRIYA.login, "writer", "internal");

  const maya = bearerClient(await machineToken(MAYA));
  for (const [id, body] of [
    [CHECKOUT_PLAN, CHECKOUT_BODY],
    [SEARCH_PLAN, SEARCH_BODY],
  ] as const) {
    await call(maya.PUT("/v1/projects/{project}/plans/{plan_id}", { params: { path: { project: PROJECT, plan_id: id } }, body: { body, area: "active" } }));
  }
  const build = await stack<{ status: string; error: string | null }>("/demo/kg", {
    project: PROJECT,
    login: MAYA.login,
    id: MAYA.id,
    plans: [CHECKOUT_BODY, SEARCH_BODY],
  });
  if (build.status !== "succeeded") throw new Error(`the demo's graph did not build: ${build.error ?? build.status}`);

  const studio = await machine(MACHINES.maya);
  const macbook = await machine(MACHINES.tomas);
  const mini = await machine(MACHINES.priya);

  const tomasRun = await stepRun(macbook, MACHINES.tomas, "2", MODELS[1], TOMAS_SESSION);
  await sendEvents(macbook, tomasRun.id, tomasTrace());
  const priyaRun = await stepRun(mini, MACHINES.priya, "3", MODELS[1], "9e2d4b7a-1c3f-4a8e-b5d6-7f0e2a9c1b38");
  await sendEvents(mini, priyaRun.id, priyaTrace());
  const mayaRun = await stepRun(studio, MACHINES.maya, "4", MODELS[0], "5a8f3c1d-7e2b-4d9a-8c6f-0b1e4d7a2c95");
  await sendEvents(studio, mayaRun.id, mayaTrace());

  const planRun = await dispatchPlan(MAYA, PROJECT, { plan_id: SEARCH_PLAN, worker_id: studio.worker.id, runtime: "claude-code", model: MODELS[0] });
  await start(studio, planRun, [mayaRun.id], "e7b3a9d1-4c2f-4e8a-9b1d-3f6c8a0e2d47", MACHINES.maya.repos);
  const trace = planRunTrace();
  await sendEvents(studio, planRun.id, trace.merged);
  const notice = await sendNotice(studio, planRun.id, NOTICE);
  await reportStep(studio, planRun.id, "3", "in_progress", API);
  await sendEvents(studio, planRun.id, trace.checkpoint);
  const { step_key: step, question, ...ask } = DECISION;
  const decision = await askDecision(studio, planRun.id, question, step, ask);
  await reportState(studio, planRun.id, { state: "waiting" });

  const runs = [tomasRun.id, priyaRun.id, mayaRun.id, planRun.id];
  const retimed = await stack<{ moved: Record<string, number> }>("/demo/retime", {
    now: new Date(NOW).toISOString(),
    rows: [
      runTimes(tomasRun.id, 25),
      runTimes(priyaRun.id, 18),
      runTimes(mayaRun.id, 11),
      runTimes(planRun.id, 47, { waiting_since: DECISION_AT, run_seconds: 41 * 60, counted_at: null }),
      { table: "decisions", where: { id: decision }, set: { asked_at: DECISION_AT } },
      { table: "notifications", where: { decision_id: decision }, set: { created_at: DECISION_AT } },
      { table: "notifications", where: { id: notice }, set: { created_at: NOTICE_AT } },
    ],
    runs,
  });
  console.log(`demo: moved the hub's own times to the story's: ${JSON.stringify(retimed.moved)}`);
  const demo = { workers: [studio.worker.id, macbook.worker.id, mini.worker.id], runs, tomasRun: tomasRun.id, planRun: planRun.id };
  await keepAlive(demo);
  return demo;
}
