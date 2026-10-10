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
export type PlanRunRequest = Schemas["PlanRunDispatch"];
export type RequestedRuntime = DispatchRequest["runtime"];
export type RunMode = DispatchRequest["mode"];
export type Approval = DispatchRequest["approval"];
export type RunKind = Run["kind"];
export type ActiveRun = Schemas["ActiveRun"];
export type PlanTimeout = PlanRunRequest["timeout_h"];

/** `runs.RUN_STATES` of the API, in its order; `satisfies` keeps the list inside the generated type. */
export const RUN_STATES = [
  "queued",
  "leased",
  "running",
  "interactive",
  "verifying",
  "waiting",
  "review",
  "parked",
  "done",
  "failed",
  "lost",
  "cancelled",
] as const satisfies readonly RunState[];
/** A worker holds the run and extends its lease (`runs.HELD_STATES`); a plan run waiting for a decision included. */
export const HELD_STATES = ["leased", "running", "interactive", "verifying", "waiting"] as const satisfies readonly RunState[];
/** At most one run of a step, or plan run of a plan, is in one of these (`runs.ACTIVE_STATES`). */
export const ACTIVE_STATES = ["queued", ...HELD_STATES, "review", "parked"] as const satisfies readonly RunState[];
export const RUNTIMES = ["claude-code", "opencode", "codex"] as const satisfies readonly RequestedRuntime[];
/** The next attempt of a run lost on a worker goes back to it once its heartbeats have come for this long, none more
 * than STEADY_GAP_SECONDS late (`runs.STEADY_SECONDS`, `runs.STEADY_GAP_SECONDS`); the run's `steady_wait` names it. */
export const STEADY_SECONDS = 120;
export const STEADY_GAP_SECONDS = 30;
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
/** `runs.PLAN_TIMEOUT_CHOICES`: hours of agent time a plan run may take; 4 by default. */
export const PLAN_TIMEOUT_CHOICES = [2, 4, 8, 24] as const satisfies readonly PlanTimeout[];
export const DEFAULT_PLAN_TIMEOUT: PlanTimeout = 4;
/** `runs.MAX_MODEL_CHARS`: a model name is one line of at most 200 characters. */
export const MAX_MODEL_CHARS = 200;
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

/**
 * Queue a plan run: one run on a worker of the caller's that does every step of the plan not done yet (409 when the plan
 * has an active run or no pending step), with the session's CSRF header.
 */
export async function dispatchPlanRun(api: ApiClient, project: string, body: PlanRunRequest): Promise<Run> {
  const headers = await csrfHeaders(api);
  return call(api.POST("/v1/projects/{project}/plan-runs", { params: { path: { project } }, body, headers }));
}

/** The active runs of one plan (its plan run, or the runs of its steps), for the plan page's banner and Run plan. */
export function planActiveRunsQuery(planId: string): RunQuery {
  return { planId, states: ACTIVE_STATES, limit: SUMMARY_LIMIT };
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

// One run: its page, its log, the owner's controls and its diff.

export type RunEvent = Schemas["RunEvent"];
export type RunEventKind = RunEvent["kind"];
export type RunEventPage = Schemas["RunEvents"];
export type RunMessage = Schemas["Message"];
export type DiffLink = Schemas["DiffLink"];

/** `runs.TERMINAL_STATES`: a run in one of these never moves again. */
export const TERMINAL_STATES = ["done", "failed", "lost", "cancelled"] as const satisfies readonly RunState[];
/** `runs.TAKEOVER_STATES` and `runs.HANDBACK_STATES`: when the owner may ask for each. */
export const TAKEOVER_STATES = ["leased", "running"] as const satisfies readonly RunState[];
export const HANDBACK_STATES = ["interactive"] as const satisfies readonly RunState[];
/** `runs.MESSAGE_STATES`: a message waits in the inbox only while an agent may still read it. */
export const MESSAGE_STATES = ["queued", ...HELD_STATES] as const satisfies readonly RunState[];
/** `runs.MAX_MESSAGE_BYTES`: a message to the agent is at most 8 KiB of UTF-8. */
export const MAX_MESSAGE_BYTES = 8 * 1024;
/** `MAX_EVENTS_PAGE` of the API: the most events one read of `events?after=` answers. */
export const EVENTS_PAGE = 1000;

export function isTerminalState(state: string): boolean {
  return (TERMINAL_STATES as readonly string[]).includes(state);
}

/** A run id as the API accepts it (`MAX_ID`, a positive bigint); null for anything else. */
export function parseRunId(text: string): number | null {
  if (!/^[1-9][0-9]{0,18}$/.test(text)) return null;
  const id = Number(text);
  return Number.isSafeInteger(id) ? id : null;
}

export function runHref(project: string, id: number): Route {
  return projectHref(project, `${RUNS_SEGMENT}/${id}`);
}

/** The search parameter of a run's page that names the tab of its log card to show first. */
export const RUN_VIEW_PARAM = "view";

/** A run's page with its Terminal tab shown, where the owner takes the run over from a decision. */
export function runTerminalHref(project: string, id: number): Route {
  return `${runHref(project, id)}?${RUN_VIEW_PARAM}=terminal` as Route;
}

export function runDiffHref(project: string, id: number): Route {
  return projectHref(project, `${RUNS_SEGMENT}/${id}/diff`);
}

/** Where a browser follows a run's events as server-sent events, on the page's own origin. */
export function runStreamPath(project: string, id: number, after: number): string {
  return `/v1/projects/${encodeURIComponent(project)}/runs/${id}/stream?after=${after}`;
}

/** Below `runKeys.all`, so a dispatch, a rerun or a control reloads it with the lists. */
export function runKey(project: string, id: number) {
  return ["projects", project, "runs", "one", id] as const;
}

/** One run, asked again every 5 seconds while it is active (its lease, a state the stream has not told yet). */
export const runQuery = (api: ApiSource, project: string, id: number) =>
  queryOptions({
    queryKey: runKey(project, id),
    queryFn: ({ signal }) =>
      call(api().GET("/v1/projects/{project}/runs/{run_id}", { params: { path: { project, run_id: id } }, signal })),
    refetchInterval: (query) => (query.state.data && isActiveState(query.state.data.state) ? LIVE_REFRESH_MS : false),
  });

export type RunLease = Schemas["RunLease"];

/** Below the run's own key, so whatever reloads the run (a move the log tells, a control) reloads its leases too. */
export function runCredentialsKey(project: string, id: number) {
  return [...runKey(project, id), "credentials"] as const;
}

/**
 * Every lease the run got, given back or not, never a value: for the member who dispatched it alone (403 for anyone
 * else, a hub admin included). Asked again every 5 seconds while the run is active, when its worker takes and gives
 * back its leases.
 */
export const runCredentialsQuery = (api: ApiSource, project: string, id: number, active: boolean) =>
  queryOptions({
    queryKey: runCredentialsKey(project, id),
    queryFn: ({ signal }) =>
      call(api().GET("/v1/projects/{project}/runs/{run_id}/credentials", { params: { path: { project, run_id: id } }, signal })),
    refetchInterval: active ? LIVE_REFRESH_MS : false,
  });

/** The run's events after `after`, in seq order: what the log reads when the stream cannot be used. */
export function runEvents(api: ApiClient, project: string, id: number, after: number, signal?: AbortSignal) {
  return call(
    api.GET("/v1/projects/{project}/runs/{run_id}/events", {
      params: { path: { project, run_id: id }, query: { after, limit: EVENTS_PAGE } },
      signal,
    }),
  );
}

/** The run's latest events the decision screen reads for "What the agent did so far". */
export const RECENT_EVENTS = 200;

/**
 * The run's events up to `lastSeq` (the run's `last_seq`, which numbers its events from 1 without gaps), at most the
 * last RECENT_EVENTS of them. A newer `lastSeq` is a new read; the summary it made stays on screen until it lands.
 */
export const recentEventsQuery = (api: ApiSource, project: string, id: number, lastSeq: number) =>
  queryOptions({
    queryKey: [...runKey(project, id), "recent", lastSeq] as const,
    queryFn: ({ signal }) => runEvents(api(), project, id, Math.max(0, lastSeq - RECENT_EVENTS), signal),
    staleTime: Infinity, // the events before a seq never change
    placeholderData: keepPreviousData,
  });

export type RunControl = "cancel" | "takeover" | "handback" | "approve" | "rerun";

/** One of the owner's controls, with the session's CSRF header; rerun answers the new run. */
export async function controlRun(api: ApiClient, project: string, id: number, action: RunControl): Promise<Run> {
  const headers = await csrfHeaders(api);
  const params = { path: { project, run_id: id } };
  switch (action) {
    case "cancel":
      return call(api.POST("/v1/projects/{project}/runs/{run_id}/cancel", { params, headers }));
    case "takeover":
      return call(api.POST("/v1/projects/{project}/runs/{run_id}/takeover", { params, headers }));
    case "handback":
      return call(api.POST("/v1/projects/{project}/runs/{run_id}/handback", { params, headers }));
    case "approve":
      return call(api.POST("/v1/projects/{project}/runs/{run_id}/approve", { params, headers }));
    case "rerun":
      return call(api.POST("/v1/projects/{project}/runs/{run_id}/rerun", { params, headers }));
  }
}

/** A message from the owner to the run's agent, which the worker hands over at the agent's next turn. */
export async function sendRunMessage(api: ApiClient, project: string, id: number, text: string): Promise<RunMessage> {
  const headers = await csrfHeaders(api);
  return call(
    api.POST("/v1/projects/{project}/runs/{run_id}/messages", { params: { path: { project, run_id: id } }, body: { text }, headers }),
  );
}

/** A presigned GET of the run's diff, working for 5 minutes; `download` asks for the attachment run-<id>.diff. */
export function runDiffLink(api: ApiClient, project: string, id: number, download = false): Promise<DiffLink> {
  return call(
    api.GET("/v1/projects/{project}/runs/{run_id}/diff", {
      params: { path: { project, run_id: id }, query: { download } },
    }),
  );
}

// The decisions a plan run's agent asks its owner (docs/notifications.md); the inbox answers them.

export type Decision = Schemas["Decision"];
export type DecisionList = Schemas["DecisionList"];

/** Where the web shows a decision and its answer form, as the hub's notifications link to it (`decision_link`). */
export function decisionHref(id: number): Route {
  return `/inbox?decision=${id}` as Route;
}

/** The id of a decision's card on its run's page, which the trace's "Asked you" links to. */
export function decisionAnchor(id: number): string {
  return `run-decision-${id}`;
}

/** `MAX_OPEN_DECISIONS` of the API: a run's agent has at most 20 decisions open at once. */
export const MAX_OPEN_DECISIONS = 20;

/**
 * The open decisions of one run, newest first, all of them: the plan's banner links to the latest, and the run's page
 * shows each with its answer form.
 */
export const openDecisionsQuery = (api: ApiSource, project: string, runId: number) =>
  queryOptions({
    queryKey: ["projects", project, "decisions", { run: runId, state: "open" }] as const,
    queryFn: ({ signal }) =>
      call(
        api().GET("/v1/projects/{project}/decisions", {
          params: { path: { project }, query: { state: ["open"], run_id: runId, limit: MAX_OPEN_DECISIONS } },
          signal,
        }),
      ),
  });

// Author runs: a plan written from a member's request on a worker of theirs, and the chat with its agent
// (docs/workers.md, Author runs).

export type AuthorRunRequest = Schemas["AuthorRunDispatch"];
export type AuthorTimeout = AuthorRunRequest["timeout_h"];
export type RunChat = Schemas["RunChat"];
export type ChatMessage = Schemas["ChatMessage"];
export type ChatStatus = RunChat["status"];

/** `author.AUTHOR_RUNTIMES`: the one runtime an author run takes. */
export const AUTHOR_RUNTIME = "claude-code" as const satisfies RequestedRuntime;
/** `author.AUTHOR_TIMEOUT_CHOICES`: hours of agent time an author run may take; 2 by default. */
export const AUTHOR_TIMEOUT_CHOICES = [1, 2, 4] as const satisfies readonly AuthorTimeout[];
export const DEFAULT_AUTHOR_TIMEOUT: AuthorTimeout = 2;
/** `author.MAX_REQUEST_BYTES`: the member's request is at most 16 KiB of UTF-8. */
export const MAX_REQUEST_BYTES = 16 * 1024;
/** While the agent works or waits, the chat is asked again this often; the run's stream also reloads it on each event. */
export const CHAT_REFRESH_MS = 3_000;

/**
 * Queue an author run on a worker of the caller's (an author run is always pinned): a new plan, or a revision of
 * `plan_id`, with the session's CSRF header.
 */
export async function dispatchAuthorRun(api: ApiClient, project: string, body: AuthorRunRequest): Promise<Run> {
  const headers = await csrfHeaders(api);
  return call(api.POST("/v1/projects/{project}/author-runs", { params: { path: { project } }, body, headers }));
}

/** Below the run's own key, so whatever reloads the run (a move the log tells, a control) reloads its chat too. */
export function chatKey(project: string, id: number) {
  return [...runKey(project, id), "chat"] as const;
}

/** The chat of an author run, over the runs that resume it: asked again every 3 seconds until it ended. */
export const chatQuery = (api: ApiSource, project: string, id: number) =>
  queryOptions({
    queryKey: chatKey(project, id),
    queryFn: ({ signal }) =>
      call(api().GET("/v1/projects/{project}/runs/{run_id}/chat", { params: { path: { project, run_id: id } }, signal })),
    refetchInterval: (query) => (query.state.data?.status === "ended" ? false : CHAT_REFRESH_MS),
  });

/** The owner's end of the chat of an author run: done at once when parked, after the agent's turn when held. */
export async function finishChat(api: ApiClient, project: string, id: number): Promise<Run> {
  const headers = await csrfHeaders(api);
  return call(api.POST("/v1/projects/{project}/runs/{run_id}/finish", { params: { path: { project, run_id: id } }, headers }));
}

/** The parameter value of a run's page that shows its chat first, as the notice author_waiting links to it. */
export const CHAT_TAB = "chat";

export function runChatHref(project: string, id: number): Route {
  return `${runHref(project, id)}?tab=${CHAT_TAB}` as Route;
}
