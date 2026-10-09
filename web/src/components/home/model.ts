import type { Route } from "next";

import { workerView } from "@/components/workers/model";
import type { Worker } from "@/components/workers/queries";
import type { components } from "@/lib/api/schema";

/**
 * How Home reads GET /v1/me/overview (web/DESIGN.md, Home): when it asks again, what the metric strip names behind each
 * number, which decisions Needs you lists, which ended runs offer Rerun, and which workers the Fleet card shows. Pure
 * functions, shared by the page and its tests.
 */
type Schemas = components["schemas"];
export type Overview = Schemas["Overview"];
export type OverviewRun = Schemas["OverviewRun"];
export type OverviewDecision = Schemas["OverviewDecision"];
export type OverviewProject = Schemas["OverviewProject"];
export type OverviewAuthorWait = Schemas["OverviewAuthorWait"];

/** While a run is in flight or a decision is open, Home asks every 5 seconds; otherwise every 30. */
export const HOME_LIVE_MS = 5_000;
export const HOME_IDLE_MS = 30_000;

/** `overview.RUNNING_STATES` of the API: an agent at work, headless or driven by a person, or its verify commands. */
export const RUNNING_STATES = ["leased", "running", "interactive", "verifying"] as const satisfies readonly OverviewRun["state"][];

export function isRunningState(state: string): boolean {
  return (RUNNING_STATES as readonly string[]).includes(state);
}

/**
 * Whether anything is in flight or waits on a person: a run queued, held, waiting or parked, an open decision, or an
 * author run whose chat waits for the visitor's reply.
 */
export function hasWork(overview: Pick<Overview, "active_runs" | "open_decisions" | "counts"> & Partial<Pick<Overview, "author_waiting">> | undefined): boolean {
  if (!overview) return false;
  const { counts } = overview;
  return (
    overview.active_runs.length > 0 ||
    overview.open_decisions.length > 0 ||
    (overview.author_waiting?.length ?? 0) > 0 ||
    counts.running > 0 ||
    counts.queued > 0 ||
    counts.waiting_on_you > 0
  );
}

/** Home's refetchInterval: 5 seconds while there is work in flight, 30 otherwise, so a dispatch made elsewhere shows. */
export function homeRefreshInterval(query: { state: { data?: Overview } }): number {
  return hasWork(query.state.data) ? HOME_LIVE_MS : HOME_IDLE_MS;
}

/**
 * The decisions Needs you lists: the open ones only the visitor may answer, the runs they dispatched. The API sends the
 * visitor's own first and the oldest first, so the order is kept. Anyone else's open decision shows in In flight, on
 * its waiting run.
 */
export function decisionsForYou(overview: Pick<Overview, "open_decisions">): OverviewDecision[] {
  return overview.open_decisions.filter((decision) => decision.yours);
}

/** The runs whose agent works now, as the API orders them: the newest first. */
export function runningRuns(overview: Pick<Overview, "active_runs">): OverviewRun[] {
  return overview.active_runs.filter((run) => isRunningState(run.state));
}

/** The queued run that has waited longest for a worker; the API sends queued runs last, the newest first. */
export function oldestQueued(overview: Pick<Overview, "active_runs">): OverviewRun | null {
  return overview.active_runs.filter((run) => run.state === "queued").at(-1) ?? null;
}

/** The run that failed last among the recent ones, for the Failed cell's line. */
export function lastFailed(overview: Pick<Overview, "recent_runs">): OverviewRun | null {
  return overview.recent_runs.find((run) => run.state === "failed") ?? null;
}

/** The open decision a waiting or parked run waits on, so In flight can say whose answer it needs. */
export function decisionOfRun(overview: Pick<Overview, "open_decisions">, run: Pick<OverviewRun, "id" | "project">): OverviewDecision | null {
  return overview.open_decisions.find((decision) => decision.run_id === run.id && decision.project === run.project) ?? null;
}

/** Ended badly: the run failed or its worker stopped extending the lease. A cancel is the owner's own choice. */
export const RERUN_STATES = ["failed", "lost"] as const satisfies readonly OverviewRun["state"][];

/**
 * Whether Recent offers Rerun on a run: one of the visitor's own that ended badly, of one step (a plan run is run again
 * with Run plan), in a project where the visitor is a writer or an admin. The API decides again (`runControls` of the
 * run page asks the same).
 */
export function canRerun(
  run: Pick<OverviewRun, "state" | "kind" | "dispatched_by" | "project">,
  viewer: string | null,
  projects: readonly Pick<OverviewProject, "name" | "role">[],
): boolean {
  if (viewer === null || run.dispatched_by !== viewer || run.kind === "plan") return false;
  if (!(RERUN_STATES as readonly string[]).includes(run.state)) return false;
  const role = projects.find((project) => project.name === run.project)?.role;
  return role === "writer" || role === "admin";
}

/** The visitor's own workers, not revoked: busy first, then idle, draining and offline, by name within each. */
export function myWorkers(workers: readonly Worker[], viewer: string | null): Worker[] {
  const order = { busy: 0, idle: 1, draining: 2, offline: 3, revoked: 4 } as const;
  return workers
    .filter((worker) => worker.owner === viewer && worker.status !== "revoked")
    .sort((a, b) => order[workerView(a)] - order[workerView(b)] || a.name.localeCompare(b.name));
}

/** Where Needs you's question links: the Inbox with the decision's sheet open, a link that can be shared or kept. */
export function decisionHref(id: number): Route {
  return `/inbox?decision=${id}` as Route;
}

/** The days of `done_by_day` as `Date`s at midnight UTC, the API's own day boundary. */
export function utcDay(day: string): Date {
  return new Date(`${day}T00:00:00Z`);
}

/** Milliseconds a run ran: from its start to its end, or to `now` while it runs; null when it never started. */
export function ranFor(run: Pick<OverviewRun, "started_at" | "finished_at">, now: number | null): number | null {
  if (!run.started_at) return null;
  const end = run.finished_at ? Date.parse(run.finished_at) : now;
  return end === null ? null : Math.max(0, end - Date.parse(run.started_at));
}
