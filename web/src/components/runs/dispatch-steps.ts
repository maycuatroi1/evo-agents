import type { ActiveRun, StepReadiness } from "./queries";

/**
 * The steps of a plan the dispatch dialog offers, those of them a dispatch picks, and the plan run that holds them.
 * Pure functions.
 */

/**
 * The steps of a plan as the dispatch dialog lists them: the pending ones in plan order, ready or not, then the
 * steps that are done, in progress or blocked, folded away. Every one of them is shown; only ready ones can be
 * picked.
 */
export function splitSteps(steps: readonly StepReadiness[]): { open: StepReadiness[]; settled: StepReadiness[] } {
  const open: StepReadiness[] = [];
  const settled: StepReadiness[] = [];
  for (const step of steps) (step.status === "pending" ? open : settled).push(step);
  return { open, settled };
}

/** The keys picked that are still ready: a step that stopped being ready while the dialog was open drops out. */
export function readySelection(selected: readonly string[], steps: readonly StepReadiness[]): string[] {
  const ready = new Set(steps.filter((step) => step.ready).map((step) => step.key));
  return selected.filter((key) => ready.has(key));
}

/** The repos the runs of the picked steps check out. */
export function selectedRepos(selected: readonly string[], steps: readonly StepReadiness[]): string[] {
  const keys = new Set(selected);
  return [...new Set(steps.filter((step) => keys.has(step.key) && step.repo).map((step) => step.repo as string))];
}

/** The plan's active plan run, as ready-steps names it: while it is active, no step of the plan is dispatched. */
export type PlanRunHold = { planId: string; run: ActiveRun };

/** The hub's reason for a step held by the plan's plan run (`runs._plan_busy`), word for word. */
export function planRunReason({ planId, run }: PlanRunHold): string {
  return `plan ${planId} has plan run #${run.id}, ${run.state}, dispatched by ${run.dispatched_by}`;
}
