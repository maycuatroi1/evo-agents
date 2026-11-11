import type { Route } from "next";

import type { Schemas } from "./run-types";

/**
 * What a Run of the Run plan dialog queued, for the toast every page may show: a plan run, or the plan handed to a
 * system. Kept apart from plan-run-choice.ts and @/lib/systems-queries, which only the dialog loads, so the shared
 * toast does not carry the systems' code into every page's first load.
 */
export type SystemDispatched = Schemas["SystemDispatched"];

/** The plan handed to a system, with the project it is in, which the hub's answer does not repeat. */
export type PlanInSystem = SystemDispatched & { project: string };
export type PlanRunOutcome = Schemas["Run"] | PlanInSystem;

export function isSystemDispatched(outcome: PlanRunOutcome): outcome is PlanInSystem {
  return "trace_id" in outcome;
}

/** Trace `id` of system `slug` in the system's Observe: the same link as `systemObserveHref(project, slug, id)`. */
export function planTraceHref(project: string, slug: string, id: number): Route {
  return `/p/${encodeURIComponent(project)}/systems/${encodeURIComponent(slug)}?mode=observe&work=${id}` as Route;
}
