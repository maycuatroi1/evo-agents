import type { Worker } from "@/components/workers/queries";
import type { PlanStep, PlanView } from "@/lib/plans";

import {
  isActiveState,
  MAX_MODEL_CHARS,
  MAX_REQUEST_BYTES,
  type RequestedRuntime,
  type Run,
  type RunState,
  RUNTIMES,
  type StepReadiness,
} from "./queries";

/**
 * How the web reads runs: a run's timing, which of the visitor's workers could take the runs a dialog dispatches, the
 * models it may name, what a plan run would do, and the worker an author run goes to. Pure functions, shared by the
 * pages and their tests. The runs list's facets and URL filters are in run-filters.ts, its summary cards in
 * runs-summary.ts and the steps the dispatch dialog offers in dispatch-steps.ts, so a page that needs one of those does
 * not load the rest.
 */

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

// Which workers could take the runs. The hub decides at claim time (runs.py, _try_claim): a worker of the member who
// dispatched, serving the project, neither draining nor revoked, with a free slot, a runtime the run asks for, and a
// checkout of the run's repo keyed <project>/<repo>, unless its daemon clones the repos it lacks (CLONES_CHECKOUTS).
// This only says what the visitor may expect.

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

/**
 * The workers a dispatch of `login` in `project` may go to: theirs and those another member shares with them, registered
 * for the project, not revoked; theirs first, then by name. The hub lists a shared worker only to the writers of its
 * projects, with the projects they hold a grant on.
 */
export function dispatchWorkers(workers: readonly Worker[], login: string, project: string): Worker[] {
  return workers
    .filter((worker) => (worker.owner === login || worker.shared) && worker.status !== "revoked" && worker.projects.includes(project))
    .sort((a, b) => Number(a.owner !== login) - Number(b.owner !== login) || a.name.localeCompare(b.name));
}

export type FitProblem =
  | { kind: "draining" }
  | { kind: "runtime"; runtime: RequestedRuntime }
  /** Every repo of the request the worker has no checkout of. */
  | { kind: "checkout"; repos: string[] }
  /** A plan run with a session for each step, which the worker's daemon does not say it runs. */
  | { kind: "sessionPerStep" };

export type WorkerFit = {
  worker: Worker;
  /** now: it can claim a run at once; busy and offline: once a slot frees or it comes back; no: not as it is. */
  fit: "now" | "busy" | "offline" | "no";
  problem: FitProblem | null;
  free: number;
};

export type FitRequest = { project: string; runtime: RequestedRuntime; repos: readonly string[]; sessionPerStep?: boolean };

/** The feature a worker's heartbeat names when its daemon runs a plan run with a new session for each step. */
export const SESSION_PER_STEP = "session_per_step";

export function runsSessionPerStep(worker: Pick<Worker, "features">): boolean {
  return (worker.features ?? []).includes(SESSION_PER_STEP);
}

/** The feature a worker's heartbeat names when its daemon clones a repo of a run it has no checkout of. */
export const CLONES_CHECKOUTS = "clones_checkouts";

export function clonesCheckouts(worker: Pick<Worker, "features">): boolean {
  return (worker.features ?? []).includes(CLONES_CHECKOUTS);
}

export function fitWorker(worker: Worker, request: FitRequest): WorkerFit {
  const free = Math.max(worker.slots - worker.held_runs, 0);
  const no = (problem: FitProblem): WorkerFit => ({ worker, fit: "no", problem, free });
  if (worker.status === "draining" || worker.drained_at !== null) return no({ kind: "draining" });
  const runtimes = workerRuntimes(worker);
  if (request.runtime === "any" ? runtimes.length === 0 : !runtimes.includes(request.runtime as RuntimeName)) {
    return no({ kind: "runtime", runtime: request.runtime });
  }
  const missing = request.repos.filter((repo) => !(`${request.project}/${repo}` in worker.checkouts));
  if (missing.length > 0 && !clonesCheckouts(worker)) return no({ kind: "checkout", repos: missing });
  if (request.sessionPerStep && !runsSessionPerStep(worker)) return no({ kind: "sessionPerStep" });
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

/**
 * Whether the hub refuses the runs of `outlook` (409, nothing dispatched): they are pinned to a worker that cannot claim
 * them as its heartbeat reports them (a runtime, checkout or session per step it lacks), as the hub's `dispatch.unfit`
 * checks it. Runs pinned to a busy, offline or draining worker wait in the queue instead.
 */
export function pinRefused(outlook: DispatchOutlook): boolean {
  return outlook.kind === "pinned" && outlook.fit.fit === "no" && outlook.fit.problem?.kind !== "draining";
}

/** Whether `outlook` says nothing is wrong: a worker takes the runs now, or there is nothing to dispatch yet. */
export function outlookFine(outlook: DispatchOutlook): boolean {
  return outlook.kind === "now" || outlook.kind === "nothing" || (outlook.kind === "pinned" && outlook.fit.fit === "now");
}

/**
 * The worker `runs` runs go to: the one picked while it is still among `workers`, null for any worker that fits when
 * that was picked, else the first of `workers` (theirs first, then by name) that could take them all now, else null,
 * which leaves the choice to the hub until a worker frees up.
 */
export function chosenWorker(workers: readonly Worker[], request: FitRequest, picked: number | "any" | null, runs = 1): number | null {
  if (picked === "any") return null;
  if (picked !== null && workers.some((worker) => worker.id === picked)) return picked;
  const found = workers.map((worker) => fitWorker(worker, request)).find((fit) => fit.fit === "now" && fit.free >= runs);
  return found ? found.worker.id : null;
}

// Models. A dispatch may name the model its runtime uses; the heartbeat reports, per runtime, the models it lists on
// the machine (`runtimes.<runtime>.models`, null when it lists none), which the dialogs offer as suggestions.

/** The models one runtime report lists, in its order; none for a report without a list. */
export function runtimeModels(report: unknown): string[] {
  if (typeof report !== "object" || report === null || Array.isArray(report)) return [];
  const models = (report as Record<string, unknown>).models;
  return Array.isArray(models) ? models.filter((model): model is string => typeof model === "string" && model.trim() !== "") : [];
}

/**
 * The models to suggest for `runtime` from what `workers` report for it, each once, sorted. Nothing for any runtime:
 * each runtime names its models its own way (opencode as provider/model), so a model goes with a runtime picked.
 */
export function modelSuggestions(workers: readonly Pick<Worker, "runtimes">[], runtime: RequestedRuntime): string[] {
  if (runtime === "any") return [];
  const found = new Set<string>();
  for (const worker of workers) {
    const report = worker.runtimes[runtime];
    if (runtimeAvailable(report)) for (const model of runtimeModels(report)) found.add(model);
  }
  return [...found].sort((a, b) => a.localeCompare(b));
}

/** Why a typed model cannot go with the dispatch: control characters, over 200 characters, or no runtime picked. */
export type ModelProblem = "line" | "long" | "runtime";

/** The model as the API takes it (trimmed; null for none), or the problem with it. */
export function readModel(text: string, runtime: RequestedRuntime): { model: string | null; problem: ModelProblem | null } {
  const model = text.trim();
  if (!model) return { model: null, problem: null };
  // The API's LINE pattern: one line, without control characters.
  if (/[\u0000-\u001f\u007f]/.test(model)) return { model, problem: "line" };
  if ([...model].length > MAX_MODEL_CHARS) return { model, problem: "long" };
  if (runtime === "any") return { model, problem: "runtime" };
  return { model, problem: null };
}

// Plan runs. A plan run is one run on one worker that does every step of the plan not done yet, in depends_on order
// (runs.py, dispatch_plan); its repos are those of the steps not done, each on the branch the plan names for it.

/** A step that is a checkpoint (a verification, a review, a deploy): flagged so, or named so in its title or first words. */
export function isCheckpoint(step: Pick<PlanStep, "title" | "what" | "extra">): boolean {
  if (step.extra.some(([key, value]) => key === "checkpoint" && value === true)) return true;
  return /\bcheck-?point\b/i.test(step.title ?? "") || /^\s*check-?point\b/i.test(step.what ?? "");
}

/** Branches a worker never pushes unless the plan names them: `gitops.PROTECTED`, plus each repo's registered default. */
export const DEFAULT_BRANCHES = ["main", "master"] as const;

export type PlanRunRepo = {
  repo: string;
  /** The branch the plan names for it; null when it names none, which fails the run before the agent starts. */
  branch: string | null;
  /** The branch is main, master or the default branch registered for the repo: the agent may push and merge into it. */
  defaultBranch: boolean;
};

export type PlanRunScope = {
  /** The steps the run does: every step not done, in plan order. */
  steps: StepReadiness[];
  pending: number;
  inProgress: number;
  blocked: number;
  repos: PlanRunRepo[];
  /** Steps not done that name no repo while the plan does not list exactly one: the hub refuses the run (409). */
  unplaced: string[];
  /** Checkpoint steps among those the run does. */
  checkpoints: PlanStep[];
};

/**
 * What a plan run of `plan` would do, from the readiness of its steps (each with the repo the hub gives it), the plan
 * itself (titles and the branches of its repos) and the default branches the project registers for its repos.
 */
export function planRunScope(
  steps: readonly StepReadiness[],
  plan: Pick<PlanView, "steps" | "repos">,
  defaults: Readonly<Record<string, string | null | undefined>> = {},
): PlanRunScope {
  const open = steps.filter((step) => step.status !== "done");
  const repos = new Map<string, PlanRunRepo>();
  const unplaced: string[] = [];
  for (const step of open) {
    if (!step.repo) {
      unplaced.push(step.key);
      continue;
    }
    if (repos.has(step.repo)) continue;
    const branch = plan.repos.find((entry) => entry.repo === step.repo)?.branch ?? null;
    const known = defaults[step.repo];
    const defaultBranch = branch !== null && ((DEFAULT_BRANCHES as readonly string[]).includes(branch) || branch === known);
    repos.set(step.repo, { repo: step.repo, branch, defaultBranch });
  }
  const keys = new Set(open.map((step) => step.key));
  return {
    steps: open,
    pending: open.filter((step) => step.status === "pending" || step.status === null).length,
    inProgress: open.filter((step) => step.status === "in_progress").length,
    blocked: open.filter((step) => step.status === "blocked").length,
    repos: [...repos.values()],
    unplaced,
    checkpoints: plan.steps.filter((step) => keys.has(step.key) && isCheckpoint(step)),
  };
}

/** The active plan run of `planId` among `runs`, or null. */
export function activePlanRun<R extends Pick<Run, "kind" | "plan_id" | "state">>(runs: readonly R[], planId: string): R | null {
  return runs.find((run) => run.kind === "plan" && run.plan_id === planId && isActiveState(run.state)) ?? null;
}

/** The ids of the Curator's plans start with this (`CURATOR_PLAN_PREFIX` of the hub's judge). */
export const CURATOR_PLAN_PREFIX = "curator-";

/** Whether `planId` is one of the Curator's plans, which the night shift runs itself at any hour: the hub refuses a dispatch (409). */
export function isCuratorPlan(planId: string): boolean {
  return planId.startsWith(CURATOR_PLAN_PREFIX);
}

/** Why Run plan is not offered for a plan now, or null when it is. */
export type PlanRunLock =
  | { kind: "curator" }
  | { kind: "planRun"; id: number; state: RunState }
  | { kind: "stepRun"; id: number; state: RunState; step: string }
  | { kind: "noPending" };

/**
 * The hub refuses a plan run (409) of a Curator's plan, and while the plan has an active run of any kind, or no pending
 * step. `runs` are runs of
 * the project (only the plan's active ones count); `pending` is how many steps of the plan are pending, or, where only
 * the counts of done steps are known (the plans list), how many are not done.
 */
export function planRunLock(
  runs: readonly Pick<Run, "id" | "kind" | "plan_id" | "state" | "step_key">[],
  planId: string,
  pending: number,
): PlanRunLock | null {
  if (isCuratorPlan(planId)) return { kind: "curator" };
  const active = runs.filter((run) => run.plan_id === planId && isActiveState(run.state));
  const plan = active.find((run) => run.kind === "plan");
  if (plan) return { kind: "planRun", id: plan.id, state: plan.state };
  const step = [...active].sort((a, b) => a.id - b.id)[0];
  if (step) return { kind: "stepRun", id: step.id, state: step.state, step: step.step_key ?? "" };
  return pending > 0 ? null : { kind: "noPending" };
}

/** How the plan pages name a plan run's state: queued, running (any state a worker drives), waiting or parked. */
export type PlanRunPhase = "queued" | "running" | "waiting" | "parked" | "review";

export function planRunPhase(state: RunState): PlanRunPhase {
  if (state === "queued" || state === "waiting" || state === "parked" || state === "review") return state;
  return "running";
}

// Author runs. An author run is one run on a worker of the member's, always pinned, that writes a plan from their
// request (runs.py, dispatch_author): its first repo is the project's harness, where plans live, which the worker must
// have a checkout of, and it runs on Claude Code alone.

/**
 * The name a worker gives the checkout of a project's harness: the last part of its path, as the hub's
 * `author.harness_repo` reads it; null for a project registered without its harness.
 */
export function harnessRepo(harness: { path: string } | null | undefined): string | null {
  if (!harness || typeof harness.path !== "string") return null;
  const name = harness.path.replaceAll("\\", "/").replace(/\/+$/, "").split("/").pop() ?? "";
  return name === "" || name === "." || name === ".." || name === "~" ? null : name;
}

/** Whether `worker` could claim an author run of `request` now. */
export function workerFitsNow(worker: Worker, request: FitRequest): boolean {
  return fitWorker(worker, request).fit === "now";
}

/**
 * The worker an author run goes to: the one picked while it is still among `workers`, else the first that could take
 * the run now, else the first that could take it at all, else the first; null without a worker.
 */
export function authorWorker(workers: readonly Worker[], request: FitRequest, picked: number | null): number | null {
  if (picked !== null && workers.some((worker) => worker.id === picked)) return picked;
  const fits = workers.map((worker) => fitWorker(worker, request));
  const best = fits.find((fit) => fit.fit === "now") ?? fits.find((fit) => fit.fit !== "no") ?? fits[0];
  return best ? best.worker.id : null;
}

/** Why a request cannot go with an author run: nothing but blanks, or over 16 KiB of UTF-8. */
export type RequestProblem = "blank" | "long";

/** The request as the API takes it (as typed: the agent reads the member's own words), or the problem with it. */
export function readRequest(text: string): { request: string; problem: RequestProblem | null } {
  if (!text.trim()) return { request: text, problem: "blank" };
  if (new TextEncoder().encode(text).length > MAX_REQUEST_BYTES) return { request: text, problem: "long" };
  return { request: text, problem: null };
}

// Task runs. A task run is one turn of a plugin's agent type on a task the plugin handed it (task_dispatch.py), on the
// branch task/<id of the run the dispatch made> of each repo; a lost run's next attempt is a new run on the same branch.

/** The branch a task run works on: the one the hub recorded, else the one its own id names (a first attempt). */
export function taskBranch(run: Pick<Run, "id" | "branch">): string {
  return run.branch ?? `task/${run.id}`;
}
