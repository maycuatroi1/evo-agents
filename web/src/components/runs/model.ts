import type { Worker } from "@/components/workers/queries";

import {
  HELD_STATES,
  isActiveState,
  MAX_QUERY,
  type RequestedRuntime,
  type Run,
  type RunList,
  type RunQuery,
  RUNS_PAGE,
  type RunState,
  RUNTIMES,
  type StateCounts,
  type StepReadiness,
} from "./queries";

/**
 * How the web reads runs: the list's facets and URL filters, the summary cards, a run's timing, the steps a dispatch
 * offers, and which of the visitor's workers could take the runs. Pure functions, shared by the pages and their tests.
 */

// The list's facets, kept in the URL. Each one is a set of states the API filters by (?state= repeated).

export const RUN_FACETS = ["active", "review", "done", "ended"] as const;
export type RunFacet = (typeof RUN_FACETS)[number];

export const FACET_STATES: Record<RunFacet, readonly RunState[]> = {
  active: ["queued", ...HELD_STATES],
  review: ["review"],
  done: ["done"],
  ended: ["failed", "lost", "cancelled"],
};

export function isRunFacet(value: string | null | undefined): value is RunFacet {
  return (RUN_FACETS as readonly string[]).includes(value ?? "");
}

export type RunFilters = {
  /** null: every state. */
  facet: RunFacet | null;
  q: string;
  /** 1-based. */
  page: number;
};

export const NO_FILTERS: RunFilters = { facet: null, q: "", page: 1 };
/** The API pages up to offset 100 000. */
export const MAX_PAGE = Math.floor(100_000 / RUNS_PAGE) + 1;

type Params = { get(name: string): string | null };

export function readFilters(source: Params): RunFilters {
  const facet = source.get("state");
  const page = Number(source.get("page") ?? "1");
  return {
    facet: isRunFacet(facet) ? facet : null,
    q: (source.get("q") ?? "").trim().slice(0, MAX_QUERY),
    page: Number.isInteger(page) && page >= 1 && page <= MAX_PAGE ? page : 1,
  };
}

export function filtersSearch(filters: RunFilters): string {
  const params = new URLSearchParams();
  if (filters.facet) params.set("state", filters.facet);
  if (filters.q) params.set("q", filters.q);
  if (filters.page > 1) params.set("page", String(filters.page));
  const text = params.toString();
  return text ? `?${text}` : "";
}

export function isFiltered(filters: RunFilters): boolean {
  return filters.facet !== null || filters.q !== "";
}

/** What the API is asked for the list a set of filters shows. */
export function listQuery(filters: RunFilters): RunQuery {
  return {
    states: filters.facet ? FACET_STATES[filters.facet] : [],
    q: filters.q,
    limit: RUNS_PAGE,
    offset: (filters.page - 1) * RUNS_PAGE,
  };
}

/** Runs a facet would show, from the counts of every state (null: all of them). */
export function facetCount(counts: StateCounts, facet: RunFacet | null): number {
  const states = facet ? FACET_STATES[facet] : (Object.keys(counts) as RunState[]);
  return states.reduce((sum, state) => sum + (counts[state] ?? 0), 0);
}

// The summary cards.

export type RunsSummary = {
  /** Runs a worker holds now, and how many workers hold them. */
  held: number;
  holdingWorkers: number;
  queued: number;
  /** ISO 8601; null when nothing waits, or the oldest waiting run is not among those read. */
  oldestQueuedAt: string | null;
  review: number;
  /** The one run waiting for review, when there is exactly one. */
  reviewRun: Run | null;
  done: number;
  failed: number;
  lost: number;
  cancelled: number;
  total: number;
};

/** The summary from the project's active runs and the counts of every state. */
export function summarizeRuns(list: RunList): RunsSummary {
  const { counts } = list;
  const held = HELD_STATES.reduce((sum, state) => sum + counts[state], 0);
  const holders = new Set<number>();
  let oldestQueuedAt: string | null = null;
  const inReview: Run[] = [];
  for (const run of list.runs) {
    if ((HELD_STATES as readonly string[]).includes(run.state) && run.worker_id !== null) holders.add(run.worker_id);
    if (run.state === "queued" && (!oldestQueuedAt || Date.parse(run.queued_at) < Date.parse(oldestQueuedAt))) {
      oldestQueuedAt = run.queued_at;
    }
    if (run.state === "review") inReview.push(run);
  }
  return {
    held,
    holdingWorkers: holders.size,
    queued: counts.queued,
    oldestQueuedAt,
    review: counts.review,
    reviewRun: counts.review === 1 && inReview.length === 1 ? inReview[0] : null,
    done: counts.done,
    failed: counts.failed,
    lost: counts.lost,
    cancelled: counts.cancelled,
    total: facetCount(counts, null),
  };
}

// A run's timing.

export type RunTiming = {
  /** When a worker took it: started, else leased; null while it waits. */
  startedAt: string | null;
  /** How long it has run (or ran); for a queued run, how long it has waited. Null when unknown. */
  durationMs: number | null;
  waiting: boolean;
};

/** `now` is the browser's clock, null on the server: a run still going has no duration until the page hydrates. */
export function runTiming(run: Pick<Run, "state" | "queued_at" | "leased_at" | "started_at" | "finished_at">, now: number | null): RunTiming {
  if (run.state === "queued") {
    return { startedAt: null, durationMs: now === null ? null : Math.max(0, now - Date.parse(run.queued_at)), waiting: true };
  }
  const startedAt = run.started_at ?? run.leased_at;
  const end = run.finished_at ? Date.parse(run.finished_at) : isActiveState(run.state) ? now : null;
  const durationMs = startedAt && end !== null ? Math.max(0, end - Date.parse(startedAt)) : null;
  return { startedAt, durationMs, waiting: false };
}

/** Whole minutes and hours of a duration, for "12 min" and "1 h 5 min". */
export function durationParts(ms: number): { hours: number; minutes: number } {
  const minutes = Math.floor(ms / 60_000);
  return { hours: Math.floor(minutes / 60), minutes: minutes % 60 };
}

// The steps a dispatch offers.

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

// Which workers could take the runs. The hub decides at claim time (runs.py, _try_claim): a worker of the member who
// dispatched, serving the project, neither draining nor revoked, with a free slot, a runtime the run asks for, and a
// checkout of the run's repo keyed <project>/<repo>. This only says what the visitor may expect.

export type RuntimeName = (typeof RUNTIMES)[number];

/** `available()` of the API: an object with available true (or without the key), true, or a version string. */
export function runtimeAvailable(report: unknown): boolean {
  if (typeof report === "object" && report !== null && !Array.isArray(report)) {
    const value = (report as Record<string, unknown>).available;
    return value === undefined || value === true;
  }
  return report === true || (typeof report === "string" && report.length > 0);
}

export function workerRuntimes(worker: Pick<Worker, "runtimes">): RuntimeName[] {
  return RUNTIMES.filter((runtime) => runtimeAvailable(worker.runtimes[runtime]));
}

/** The workers a dispatch of `login` in `project` may go to: theirs, registered for the project, not revoked. */
export function dispatchWorkers(workers: readonly Worker[], login: string, project: string): Worker[] {
  return workers
    .filter((worker) => worker.owner === login && worker.status !== "revoked" && worker.projects.includes(project))
    .sort((a, b) => a.name.localeCompare(b.name));
}

export type FitProblem =
  | { kind: "draining" }
  | { kind: "runtime"; runtime: RequestedRuntime }
  | { kind: "checkout"; repo: string };

export type WorkerFit = {
  worker: Worker;
  /** now: it can claim a run at once; busy and offline: once a slot frees or it comes back; no: not as it is. */
  fit: "now" | "busy" | "offline" | "no";
  problem: FitProblem | null;
  free: number;
};

export type FitRequest = { project: string; runtime: RequestedRuntime; repos: readonly string[] };

export function fitWorker(worker: Worker, request: FitRequest): WorkerFit {
  const free = Math.max(worker.slots - worker.held_runs, 0);
  const no = (problem: FitProblem): WorkerFit => ({ worker, fit: "no", problem, free });
  if (worker.status === "draining" || worker.drained_at !== null) return no({ kind: "draining" });
  const runtimes = workerRuntimes(worker);
  if (request.runtime === "any" ? runtimes.length === 0 : !runtimes.includes(request.runtime as RuntimeName)) {
    return no({ kind: "runtime", runtime: request.runtime });
  }
  const missing = request.repos.find((repo) => !(`${request.project}/${repo}` in worker.checkouts));
  if (missing !== undefined) return no({ kind: "checkout", repo: missing });
  if (worker.status !== "online") return { worker, fit: "offline", problem: null, free };
  return { worker, fit: free > 0 ? "now" : "busy", problem: null, free };
}

/** How many of `workers` report `runtime` (any: at least one runtime). */
export function runtimeCount(workers: readonly Worker[], runtime: RequestedRuntime): number {
  return workers.filter((worker) => {
    const runtimes = workerRuntimes(worker);
    return runtime === "any" ? runtimes.length > 0 : runtimes.includes(runtime as RuntimeName);
  }).length;
}

export type DispatchOutlook =
  | { kind: "nothing" }
  | { kind: "noWorker" }
  | { kind: "pinned"; fit: WorkerFit }
  | { kind: "now"; fits: WorkerFit[] }
  | { kind: "later"; fits: WorkerFit[] }
  | { kind: "none"; fits: WorkerFit[] };

/** What the dialog's footer says will happen to the runs, from the workers that may take them. */
export function dispatchOutlook(
  runs: number,
  workers: readonly Worker[],
  request: FitRequest,
  pinned: number | null,
): DispatchOutlook {
  if (runs === 0) return { kind: "nothing" };
  if (pinned !== null) {
    const worker = workers.find((candidate) => candidate.id === pinned);
    return worker ? { kind: "pinned", fit: fitWorker(worker, request) } : { kind: "noWorker" };
  }
  if (workers.length === 0) return { kind: "noWorker" };
  const fits = workers.map((worker) => fitWorker(worker, request));
  const now = fits.filter((fit) => fit.fit === "now");
  if (now.length) return { kind: "now", fits: now };
  const later = fits.filter((fit) => fit.fit === "busy" || fit.fit === "offline");
  if (later.length) return { kind: "later", fits: later };
  return { kind: "none", fits };
}
