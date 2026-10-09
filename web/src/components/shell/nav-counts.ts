"use client";

import { useQuery } from "@tanstack/react-query";

import type { Overview } from "@/components/home/model";
import { notificationCountQuery } from "@/components/inbox/queries";
import { ACTIVE_STATES, type RunList, runsSummaryQuery } from "@/components/runs/queries";
import { workerView } from "@/components/workers/model";
import { type Worker, workersQuery } from "@/components/workers/queries";
import { browserApi } from "@/lib/api/browser";
import { overviewQuery } from "@/lib/queries";

import type { NavCount } from "./nav";

/**
 * What the shell counts, read through the same queries as the pages that show the full picture, so a page and the
 * sidebar share one cache entry and one poll: the bell's count (every 10 seconds), the runs summary of the project
 * (every 5 seconds while a run is active, 30 otherwise), the overview of Home and the Monitor (every 15 seconds while
 * a run is at work, 30 otherwise; those pages ask it more often) and the workers list (every 10 seconds).
 */

/** How often the sidebar asks the overview for its Monitor count: Home and the Monitor ask every 5 seconds. */
export const RUNNING_COUNT_LIVE_MS = 15_000;
export const RUNNING_COUNT_IDLE_MS = 30_000;

export function runningCountInterval(query: { state: { data?: Pick<Overview, "counts"> } }): number {
  return (query.state.data?.counts.running ?? 0) > 0 ? RUNNING_COUNT_LIVE_MS : RUNNING_COUNT_IDLE_MS;
}

/** Runs of the project in an active state (`runs.ACTIVE_STATES`): queued, held by a worker, or waiting on a person. */
export function activeRunCount(list: Pick<RunList, "counts">): number {
  return ACTIVE_STATES.reduce((sum, state) => sum + list.counts[state], 0);
}

/** How the fleet line reads the visitor's workers: registered (not revoked), online (idle or busy), busy. */
export type Fleet = { registered: number; online: number; busy: number };

export function fleetOf(workers: readonly Pick<Worker, "status" | "held_runs">[]): Fleet {
  const fleet: Fleet = { registered: 0, online: 0, busy: 0 };
  for (const worker of workers) {
    const view = workerView(worker);
    if (view === "revoked") continue;
    fleet.registered += 1;
    if (view === "idle" || view === "busy") fleet.online += 1;
    if (view === "busy") fleet.busy += 1;
  }
  return fleet;
}

/** The decisions and the Curator's proposals waiting for the visitor's answer; null until the hub has said. */
export function useOpenDecisions(): number | null {
  const { data } = useQuery(notificationCountQuery(browserApi));
  return data ? data.open_decisions + (data.open_proposals ?? 0) : null;
}

/** The project's active runs; null outside a project, until the hub has said, or when the visitor may not list them. */
export function useActiveRuns(project: string | null): number | null {
  const { data } = useQuery({ ...runsSummaryQuery(browserApi, project ?? ""), enabled: project !== null });
  return project !== null && data ? activeRunCount(data) : null;
}

/** The runs at work (`overview.RUNNING_STATES`) in every project of the visitor's grants; null until the hub has said. */
export function useRunningRuns(): number | null {
  const { data } = useQuery({ ...overviewQuery(browserApi), refetchInterval: runningCountInterval });
  return data ? data.counts.running : null;
}

/** The value of each kind of count in the sidebar. */
export function useNavCounts(project: string | null): Record<NavCount, number | null> {
  return { openDecisions: useOpenDecisions(), activeRuns: useActiveRuns(project), runningRuns: useRunningRuns() };
}

export type FleetState = { status: "loading" } | { status: "error" } | { status: "ready"; fleet: Fleet };

/** The visitor's workers as the fleet line shows them (GET /v1/workers: their own, or every worker for a hub admin). */
export function useFleet(): FleetState {
  const { data, isError } = useQuery(workersQuery(browserApi));
  if (data) return { status: "ready", fleet: fleetOf(data) };
  return isError ? { status: "error" } : { status: "loading" };
}
