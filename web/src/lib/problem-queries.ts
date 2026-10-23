import { queryOptions } from "@tanstack/react-query";

import { call } from "@/lib/api/client";
import type { components } from "@/lib/api/schema";
import type { ApiSource } from "@/lib/queries";

export { PROBLEM_REF, PROBLEM_STATUSES, type ProblemStatus, problemsHref, problemsOf } from "@/lib/problem-links";

/**
 * The reads of a project's problems (plan problem-oriented-hub; `evo_agents/hub/server/problems.py`): what hurts,
 * before any solution, by code P-<number>. Any member with a grant reads them. The Problems pages read them, and the
 * chips of the plans, goal and claim pages name them, so the reads live here; the writes, which only the Problems
 * pages make, are `lib/problem-writes.ts`.
 */
type Schemas = components["schemas"];
export type ProjectProblem = Schemas["ProjectProblem"];
export type ProjectProblemShown = Schemas["ProjectProblemShown"];
export type ProblemRevision = Schemas["ProblemRevision"];
export type ProblemMetric = Schemas["ProblemMetric"];
export type ProblemGoal = Schemas["ProblemGoal"];
export type ProblemClaim = Schemas["ProblemClaim"];
export type ProblemPlan = Schemas["ProblemPlan"];
export type ProblemAggregate = ProblemMetric["aggregate"];

export const problemKeys = {
  /** Every read of a project's problems: invalidated after a write of one. */
  all: (project: string) => ["projects", project, "problems"] as const,
  list: (project: string) => ["projects", project, "problems", "list"] as const,
  one: (project: string, code: string) => ["projects", project, "problems", "one", code] as const,
  revisions: (project: string, code: string) => ["projects", project, "problems", "revisions", code] as const,
};

/** The project's problems as they are now, of every status, by rank. */
export const problemsQuery = (api: ApiSource, project: string) =>
  queryOptions({
    queryKey: problemKeys.list(project),
    queryFn: ({ signal }) => call(api().GET("/v1/projects/{project}/problems", { params: { path: { project } }, signal })),
  });

/** One problem at its newest revision, with the goals that answer it and the claims and plans that point to it. */
export const problemQuery = (api: ApiSource, project: string, code: string) =>
  queryOptions({
    queryKey: problemKeys.one(project, code),
    queryFn: ({ signal }) =>
      call(api().GET("/v1/projects/{project}/problems/{ref}", { params: { path: { project, ref: code } }, signal })),
  });

/** Every revision of a problem, newest first, each with the fields it changed. */
export const problemRevisionsQuery = (api: ApiSource, project: string, code: string) =>
  queryOptions({
    queryKey: problemKeys.revisions(project, code),
    queryFn: ({ signal }) =>
      call(api().GET("/v1/projects/{project}/problems/{ref}/revisions", { params: { path: { project, ref: code } }, signal })),
  });
