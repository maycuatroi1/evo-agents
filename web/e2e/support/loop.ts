import { call } from "../../src/lib/api/client";
import type { components } from "../../src/lib/api/schema";

import { CANVAS_SLUG, METRIC, pushConversions, seedCanvas } from "./canvas";
import { STACK_URL } from "./env";
import { type Account, bearerClient, machineToken } from "./hub";
import { putPolicy } from "./merge-queue";
import { enableSrs, enableTasks, importSample } from "./srs";
import { createTaskByApi } from "./tasks";

/**
 * A project whose Loop worked toward a goal for a few weeks (plan loop-engine), for the specs of the Goals, Loop,
 * chain and Decisions pages: its canvas, SRS document, frame task, plans, goals and policy through the real API, as
 * `evo-agents hub goal set` and `hub policy set` write them, and its work items as the Loop's tick would leave them
 * (hub_stack POST /loop/seed, `e2e/loop_seed.py`), since the stack runs no worker. Names are fictional.
 */
export type LoopSeed = { items: Record<string, number>; merges: Record<string, number> };
export type GoalWrite = components["schemas"]["ProjectGoalWrite"];

export const RELEASED_PLAN = "northwind-one-tap";
export const HELD_PLAN = "northwind-reorder";
/** G-1: checkout conversion, whose mean over 30 days is under its target (the conversions of `pushConversions`). */
export const GOAL = { statement: "Shops check out in one tap", metric: { key: METRIC, aggregate: "mean", window_days: 30, comparator: ">=", target: 0.6 }, canvas: CANVAS_SLUG, status: "active" } satisfies GoalWrite;
/** G-2: no metric, past its deadline. */
export const LATE_GOAL = { statement: "Shops reorder every week", deadline: "2026-01-31", status: "active" } satisfies GoalWrite;

export function goalsPath(project: string, code = ""): string {
  return `/p/${project}/goals${code ? `/${code}` : ""}`;
}

export function loopPath(project: string, rest = ""): string {
  return `/p/${project}/loop${rest}`;
}

export function itemPath(project: string, id: number): string {
  return loopPath(project, `/items/${id}`);
}

export function decidePath(project: string): string {
  return `/p/${project}/decide`;
}

async function putGoal(account: Account, project: string, ref: string, body: GoalWrite) {
  const api = bearerClient(await machineToken(account));
  return call(api.PUT("/v1/projects/{project}/goals/{ref}", { params: { path: { project, ref } }, body }));
}

async function putPlan(account: Account, project: string, id: string, goal: string) {
  const api = bearerClient(await machineToken(account));
  const body = { id, goal, repos: [{ repo: "api" }], steps: [{ id: 1, title: "Change api", repo: "api", what: "the change in api", status: "done" }] };
  return call(api.PUT("/v1/projects/{project}/plans/{plan_id}", { params: { path: { project, plan_id: id } }, body: { body } }));
}

/**
 * `project` as `owner`, an admin of it, would leave it: the canvas of northwind with claim CL-1 testing and three
 * conversions under G-1's target; SRS document demo-shop; the frame task T-1; the plans released and held; goals G-1
 * and G-2; the Loop turned on.
 */
export async function seedLoopProject(owner: Account, project: string): Promise<void> {
  await seedCanvas(owner, project);
  await pushConversions(owner, project);
  await enableSrs(owner, project);
  await importSample(owner, project);
  await enableTasks(owner, project);
  await createTaskByApi(owner, project, { title: "Place an order", status: "todo" });
  await putPlan(owner, project, RELEASED_PLAN, "Shops check out in one tap.");
  await putPlan(owner, project, HELD_PLAN, "Shops reorder with one button.");
  await putGoal(owner, project, "G-1", GOAL);
  await putGoal(owner, project, "G-2", LATE_GOAL);
  await putPolicy(owner, project, { loop: { enabled: true } }, "the Loop works toward G-1");
}

/** The project's work items as the Loop would leave them; with `tripped` its breaker trips. */
export async function seedLoop(project: string, { tripped = false }: { tripped?: boolean } = {}): Promise<LoopSeed> {
  const response = await fetch(`${STACK_URL}/loop/seed`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ project, released: RELEASED_PLAN, held: HELD_PLAN, tripped }),
  });
  if (!response.ok) throw new Error(`hub_stack /loop/seed: ${response.status} ${await response.text()}`);
  return (await response.json()) as LoopSeed;
}
