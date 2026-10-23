import { type ApiClient, call } from "@/lib/api/client";
import { csrfHeaders } from "@/lib/api/csrf";
import type { components } from "@/lib/api/schema";
import { PROBLEM_REF, type ProblemStatus } from "@/lib/problem-links";
import type { ProblemAggregate, ProjectProblem } from "@/lib/problem-queries";

/**
 * The writes of a project's problems (plan problem-oriented-hub): a writer of the project adds one, writes one whole as
 * its next revision, closes one as solved or dropped and reopens one; the agent of a run never writes one. With the
 * hub's limits, so a form refuses what the hub would. Only the Problems pages write, so the pages that only name a
 * problem do not load this.
 */
type Schemas = components["schemas"];
export type ProblemWrite = Schemas["ProjectProblemWrite"];
export type ProblemClose = Schemas["ProjectProblemClose"];

export const CLOSED_STATUSES = ["solved", "dropped"] as const satisfies readonly ProblemStatus[];
/** `problems.AGGREGATES`, as a goal's metric sums its key's signals up. */
export const PROBLEM_AGGREGATES = ["last", "mean", "sum", "count", "p50", "p90"] as const satisfies readonly ProblemAggregate[];

/** The limits of `evo_agents.hub.problems`, so a form refuses what the hub would. */
export const PROBLEM_LIMITS = {
  statement: 2000,
  text: 2000,
  reason: 1000,
  note: 1000,
  rank: 10_000,
  windowDays: 90,
  tenant: 100,
} as const;
/** `problems.DEFAULT_WINDOW_DAYS`: the window of a metric that names none. */
export const DEFAULT_WINDOW_DAYS = 90;
/** `admin.LOGIN_NAME`: an owner's login. */
export const LOGIN_NAME = /^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$/;

/** The number of a problem's code, or null for another text. */
export function problemNumber(code: string): number | null {
  return PROBLEM_REF.test(code) ? Number(code.slice(2)) : null;
}

/**
 * The code the project's next problem takes: one after the highest it ever gave, since a code is never given again
 * and a problem is never deleted. The hub checks it again (422 for another code).
 */
export function nextProblemCode(problems: readonly Pick<ProjectProblem, "number">[]): string {
  return `P-${problems.reduce((highest, problem) => Math.max(highest, problem.number), 0) + 1}`;
}

// Writes: a writer of the project; each fetches the session's CSRF header first, as every write of the web does.

/**
 * Write problem `code` whole as its next revision: a new one with `if_revision` 0, an edit at the revision the page
 * read. The hub answers 409 when another writer wrote it since, and 422 naming each field it refused.
 */
export async function writeProblem(api: ApiClient, project: string, code: string, body: ProblemWrite) {
  const headers = await csrfHeaders(api);
  return call(api.PUT("/v1/projects/{project}/problems/{ref}", { params: { path: { project, ref: code } }, body, headers }));
}

/** Close an open problem as solved or dropped, for a reason, as its next revision. */
export async function closeProblem(api: ApiClient, project: string, code: string, body: ProblemClose) {
  const headers = await csrfHeaders(api);
  return call(api.POST("/v1/projects/{project}/problems/{ref}/close", { params: { path: { project, ref: code } }, body, headers }));
}

/** The write that reopens a closed problem: its fields as they are, open again, without the reason it was closed. */
export function reopening(problem: ProjectProblem, note: string): ProblemWrite {
  return {
    statement: problem.statement,
    who: problem.who,
    when: problem.when,
    workaround: problem.workaround,
    metric: problem.metric,
    rank: problem.rank,
    owner: problem.owner,
    status: "open",
    close_reason: null,
    note: note.trim() || null,
    if_revision: problem.revision,
  };
}
