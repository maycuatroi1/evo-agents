import type { Route } from "next";

import type { components } from "@/lib/api/schema";

/**
 * Where a project's problems are, and how their codes and statuses read (plan problem-oriented-hub): what the pages
 * that only name a problem need (a chain's rail, the chips of the plans, goal and claim pages), apart from the reads
 * and writes of `lib/problem-queries.ts`.
 */
export type ProblemStatus = components["schemas"]["ProjectProblem"]["status"];

/** `problems.STATUSES`: an open problem, then the two ways one closes. */
export const PROBLEM_STATUSES = ["open", "solved", "dropped"] as const satisfies readonly ProblemStatus[];
/** `problems.REF`: a problem's code. */
export const PROBLEM_REF = /^P-[1-9][0-9]{0,8}$/;

export function problemsHref(project: string, code?: string): Route {
  const base = `/p/${encodeURIComponent(project)}/problems`;
  return (code ? `${base}/${encodeURIComponent(code)}` : base) as Route;
}

/** The problems a plan's body names (`problems: [P-1]`), each once, in its order; none for a body without. */
export function problemsOf(body: unknown): string[] {
  if (typeof body !== "object" || body === null) return [];
  const listed = (body as { problems?: unknown }).problems;
  if (!Array.isArray(listed)) return [];
  return [...new Set(listed.filter((code): code is string => typeof code === "string" && PROBLEM_REF.test(code)))];
}
