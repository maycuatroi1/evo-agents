import { call } from "../../src/lib/api/client";
import type { components } from "../../src/lib/api/schema";

import { claimByApi, editByApi, METRIC, TENANT } from "./canvas";
import { type Account, bearerClient, machineToken } from "./hub";
import { GOAL, type LoopSeed, putGoal, putPlan, RELEASED_PLAN, seedLoop, seedLoopProject } from "./loop";

/**
 * The problems of a project (plan problem-oriented-hub) through the real API, as `evo-agents hub problem set` writes
 * them with a writer's machine token, and a project whose goal, claim, plan and Loop all go from one of them, for the
 * specs of the Problems pages. Names are fictional.
 */
export type ProblemWrite = components["schemas"]["ProjectProblemWrite"];

/** P-1: what CL-1 of the canvas says, measured by the conversions of lumen-shops over 30 days. */
export const PROBLEM = {
  statement: "Shop owners lose a sale when checkout takes more than two minutes",
  who: "Owners of small shops on lumen-shops",
  when: "At lunch, when the queue at the till grows",
  workaround: "They write the order on paper and key it in at night",
  metric: { key: METRIC, aggregate: "mean", window_days: 30, tenant: TENANT },
  status: "open",
} satisfies ProblemWrite;

export function problemsPath(project: string, code = ""): string {
  return `/p/${project}/problems${code ? `/${code}` : ""}`;
}

/** Write problem `code` whole as `account`, a writer of the project. */
export async function putProblemByApi(account: Account, project: string, code: string, body: ProblemWrite) {
  const api = bearerClient(await machineToken(account));
  return call(api.PUT("/v1/projects/{project}/problems/{ref}", { params: { path: { project, ref: code } }, body }));
}

/**
 * `project` as `owner`, an admin of it, would leave it once the Loop goes from problem P-1: the project of
 * `seedLoopProject`, then P-1, which G-1 answers, CL-1 links and plan northwind-one-tap names, and the work items of
 * `seedLoop` going from it, with a discover of CL-1 running.
 */
export async function seedProblems(owner: Account, project: string): Promise<LoopSeed> {
  await seedLoopProject(owner, project);
  await putProblemByApi(owner, project, "P-1", PROBLEM);
  await putGoal(owner, project, "G-1", { ...GOAL, problem: "P-1" });
  const claim = await claimByApi(owner, project, "CL-1");
  await editByApi(owner, project, [{ claim: "CL-1", base_rev: claim.rev, problems: ["P-1"] }], "the problem it states");
  await putPlan(owner, project, RELEASED_PLAN, "Shops check out in one tap.", ["P-1"], 1);
  return seedLoop(project, { problem: "P-1" });
}
