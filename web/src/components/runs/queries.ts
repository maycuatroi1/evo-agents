import { keepPreviousData, queryOptions } from "@tanstack/react-query";
import type { Route } from "next";

import { projectHref } from "@/components/shell/nav";
import { type ApiClient, call } from "@/lib/api/client";
import { csrfHeaders } from "@/lib/api/csrf";
import type { components } from "@/lib/api/schema";
import type { ApiSource } from "@/lib/queries";

/**
 * What the runs pages read and write (docs/workers.md). Any member with a grant on the project lists its runs, of the
 * plans the member may read; dispatching needs the writer role, and a pinned worker must be the caller's own. A hub
 * admin without a grant gets 403 like anyone else: the hub admin role never dispatches.
 */
type Schemas = components["schemas"];
export type Run = Schemas["Run"];
export type RunList = Schemas["RunList"];
export type RunState = Run["state"];
export type StateCounts = Schemas["StateCounts"];
export type ReadySteps = Schemas["ReadySteps"];
export type StepReadiness = Schemas["StepReadiness"];
export type DispatchRequest = Schemas["Dispatch"];
export type RequestedRuntime = DispatchRequest["runtime"];
export type RunMode = DispatchRequest["mode"];
export type Approval = DispatchRequest["approval"];

/** `runs.RUN_STATES` of the API, in its order; `satisfies` keeps the list inside the generated type. */
export const RUN_STATES = [
  "queued",
  "leased",
  "running",
  "interactive",
  "verifying",
  "review",
  "done",
  "failed",
  "lost",
  "cancelled",
] as const satisfies readonly RunState[];
/** A worker holds the run and extends its lease. */
export const HELD_STATES = ["leased", "running", "interactive", "verifying"] as const satisfies readonly RunState[];
/** At most one run of a step is in one of these (`runs.ACTIVE_STATES`). */
export const ACTIVE_STATES = ["queued", ...HELD_STATES, "review"] as const satisfies readonly RunState[];
export const RUNTIMES = ["claude-code", "opencode", "codex"] as const satisfies readonly RequestedRuntime[];
export const MODES = ["headless", "interactive"] as const satisfies readonly RunMode[];
export const APPROVALS = ["review", "auto"] as const satisfies readonly Approval[];

/** While a run is active the pages ask every 5 seconds; otherwise every 30, to catch a dispatch made elsewhere. */
export const LIVE_REFRESH_MS = 5_000;
export const IDLE_REFRESH_MS = 30_000;
/** Runs on one page of the list; the API allows up to 200. */
export const RUNS_PAGE = 50;
/** The active runs the summary reads to tell how many workers hold them and how old the queue is. */
export const SUMMARY_LIMIT = 200;
/** `timeout_min` of a dispatch: 5 to 240 minutes, 60 by default. */
export const TIMEOUT_CHOICES = [30, 60, 120, 240] as const;
export const DEFAULT_TIMEOUT = 60;
/** `MAX_DISPATCH_STEPS` of the API. */
export const MAX_DISPATCH_STEPS = 50;
/** `max_length` of the list's q. */
export const MAX_QUERY = 200;

const ACTIVE = new Set<string>(ACTIVE_STATES);

export function isActiveState(state: string): boolean {
  return ACTIVE.has(state);
}

/** Whether a list's counts hold any active run of the project: the pages then refresh every 5 seconds. */
export function hasActiveRuns(list: Pick<RunList, "counts"> | undefined): boolean {
  if (!list) return false;
  return ACTIVE_STATES.some((state) => list.counts[state] > 0);
}

/**
 * How often a list asks again: every 5 seconds while its counts hold an active run, every 30 otherwise. The counts
 * leave out the state filter, so a list filtered to finished runs still refreshes quickly while others are active.
 */
export function refreshInterval(query: { state: { data?: RunList } }): number {
  return hasActiveRuns(query.state.data) ? LIVE_REFRESH_MS : IDLE_REFRESH_MS;
}

/** The filters of GET /v1/projects/{p}/runs that the pages use. */
export type RunQuery = {
  states?: readonly RunState[];
  planId?: string | null;
  step?: string | null;
  workerId?: number | null;
  q?: string;
  limit?: number;
  offset?: number;
};

function apiQuery(query: RunQuery) {
  return {
    ...(query.states && query.states.length ? { state: [...query.states] } : {}),
    ...(query.planId ? { plan_id: query.planId } : {}),
    ...(query.step ? { step: query.step } : {}),
    ...(query.workerId ? { worker_id: query.workerId } : {}),
    ...(query.q ? { q: query.q } : {}),
    limit: query.limit ?? RUNS_PAGE,
    offset: query.offset ?? 0,
  };
}

/** A query's filters in a stable shape, so the server's prefetch and the browser share one cache entry. */
function keyOf(query: RunQuery) {
  return {
    states: [...(query.states ?? [])],
    planId: query.planId ?? null,
    step: query.step ?? null,
    workerId: query.workerId ?? null,
    q: query.q ?? "",
    limit: query.limit ?? RUNS_PAGE,
    offset: query.offset ?? 0,
  };
}

export const runKeys = {
  /** Every run query of the project: invalidated after a dispatch. */
  all: (project: string) => ["projects", project, "runs"] as const,
  list: (project: string, query: RunQuery) => ["projects", project, "runs", "list", keyOf(query)] as const,
  summary: (project: string) => ["projects", project, "runs", "summary"] as const,
  /** Below the plan's own key (`planKeys.one`), so reloading the plan reloads its readiness too. */
  ready: (project: string, plan: string) => ["projects", project, "plans", plan, "ready-steps"] as const,
};

/** One page of the project's runs, newest first, with the count of each state under the other filters. */
export const runsQuery = (api: ApiSource, project: string, query: RunQuery) =>
  queryOptions({
    queryKey: runKeys.list(project, query),
    queryFn: ({ signal }) =>
      call(api().GET("/v1/projects/{project}/runs", { params: { path: { project }, query: apiQuery(query) }, signal })),
    placeholderData: keepPreviousData, // the last page stays on screen while the next filter or page loads
    refetchInterval: refreshInterval,
  });

/**
 * The project's active runs (and, through `counts`, how many runs each state holds overall): the summary cards, and
 * whether the pages refresh every 5 seconds.
 */
export const runsSummaryQuery = (api: ApiSource, project: string) =>
  queryOptions({
    queryKey: runKeys.summary(project),
    queryFn: ({ signal }) =>
      call(
        api().GET("/v1/projects/{project}/runs", {
          params: { path: { project }, query: apiQuery({ states: ACTIVE_STATES, limit: SUMMARY_LIMIT }) },
          signal,
        }),
      ),
    refetchInterval: refreshInterval,
  });

/** Every step of the plan, in plan order, with whether a dispatch of it would be taken now and why not. */
export const readyStepsQuery = (api: ApiSource, project: string, plan: string) =>
  queryOptions({
    queryKey: runKeys.ready(project, plan),
    queryFn: ({ signal }) =>
      call(
        api().GET("/v1/projects/{project}/plans/{plan_id}/ready-steps", {
          params: { path: { project, plan_id: plan } },
          signal,
        }),
      ),
    staleTime: 0, // readiness changes as other runs move; ask each time a dialog or step page needs it
  });

/** Queue one run per step named, all of them or none (409 when a step is not ready), with the session's CSRF header. */
export async function dispatchRuns(api: ApiClient, project: string, body: DispatchRequest): Promise<Run[]> {
  const headers = await csrfHeaders(api);
  return call(api.POST("/v1/projects/{project}/runs", { params: { path: { project } }, body, headers }));
}

/** Runs of one step listed on its page; the full history is on the project's Runs page. */
export const STEP_RUNS = 10;

/** The latest runs of one step, for its page (prefetched by the server, read again in the browser). */
export function stepRunsQuery(planId: string, stepKey: string): RunQuery {
  return { planId, step: stepKey, limit: STEP_RUNS };
}

export const RUNS_SEGMENT = "runs";

export function runsHref(project: string): Route {
  return projectHref(project, RUNS_SEGMENT);
}
