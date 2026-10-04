import type { Route } from "next";

import { projectHref } from "@/components/shell/nav";

/** Where the plan pages live, below /p/{project}/plans. */
export const PLANS_SEGMENT = "plans";

export function plansHref(project: string): Route {
  return projectHref(project, PLANS_SEGMENT);
}

export function planHref(project: string, plan: string): Route {
  return projectHref(project, `${PLANS_SEGMENT}/${encodeURIComponent(plan)}`);
}

export function stepHref(project: string, plan: string, step: string): Route {
  return projectHref(project, `${PLANS_SEGMENT}/${encodeURIComponent(plan)}/steps/${encodeURIComponent(step)}`);
}

export function revisionsHref(project: string, plan: string, pair?: { from: number; to: number }): Route {
  const base = projectHref(project, `${PLANS_SEGMENT}/${encodeURIComponent(plan)}/revisions`);
  return (pair ? `${base}?from=${pair.from}&to=${pair.to}` : base) as Route;
}
