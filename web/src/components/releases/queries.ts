import { keepPreviousData, queryOptions } from "@tanstack/react-query";
import type { Route } from "next";

import { projectHref } from "@/components/shell/nav";
import { type ApiClient, call } from "@/lib/api/client";
import { csrfHeaders } from "@/lib/api/csrf";
import type { components } from "@/lib/api/schema";
import type { ApiSource } from "@/lib/queries";

/**
 * What the Releases page reads and writes (docs/releases.md): a project's daily release, its schedule, its latest
 * checks and the cycle open now (GET .../releases), and the cycles that ended (GET .../releases/cycles). Any member with
 * a grant reads them; only an admin of the project pauses, resumes or runs a check now, and the API decides again on
 * every write.
 */
type Schemas = components["schemas"];
export type ReleaseStatus = Schemas["ReleaseStatus"];
export type ReleaseSchedule = Schemas["ReleaseSchedule"];
export type ReleaseCheck = Schemas["ReleaseCheck"];
export type ReleaseCycle = Schemas["ReleaseCycle"];
export type ReleaseCycleRun = Schemas["ReleaseCycleRun"];
export type ReleaseTag = Schemas["ReleaseTag"];
export type ReleaseCycleList = Schemas["ReleaseCycleList"];
export type CheckOutcome = ReleaseCheck["outcome"];
export type CiState = ReleaseTag["ci_state"];
export type CycleState = ReleaseCycle["state"];
export type StopCause = NonNullable<ReleaseCycle["stop_cause"]>;

/** The page reads the release again this often while a cycle has work in flight, and this often otherwise. */
export const RELEASES_LIVE_MS = 10_000;
export const RELEASES_IDLE_MS = 60_000;
/** The ended cycles on one page of the list; the API allows up to 100. */
export const CYCLES_PAGE = 20;

export const releaseKeys = {
  /** Everything the Releases page reads of a project: invalidated after a pause, a resume or a check. */
  all: (project: string) => ["projects", project, "releases"] as const,
  status: (project: string) => ["projects", project, "releases", "status"] as const,
  cycles: (project: string, offset: number) => ["projects", project, "releases", "cycles", offset] as const,
};

export const RELEASES_SEGMENT = "releases";

export function releasesHref(project: string): Route {
  return projectHref(project, RELEASES_SEGMENT);
}

export function runHref(project: string, id: number): Route {
  return projectHref(project, `runs/${id}`);
}

/** Whether the cycle has work in flight: a run of it queued or under way, or a tag whose CI the hub still reads. */
export function inFlight(cycle: ReleaseCycle | null | undefined): boolean {
  if (!cycle || cycle.state !== "open") return false;
  const ended = new Set(["done", "failed", "lost", "cancelled"]);
  return cycle.runs.some((run) => !ended.has(run.state)) || cycle.tags.some((tag) => tag.ci_state === "pending" || tag.ci_state === "running");
}

/** The release: read every 10 seconds while its cycle has work in flight, every minute otherwise. */
export const releaseStatusQuery = (api: ApiSource, project: string) =>
  queryOptions({
    queryKey: releaseKeys.status(project),
    queryFn: ({ signal }) => call(api().GET("/v1/projects/{project}/releases", { params: { path: { project } }, signal })),
    refetchInterval: (query) => (inFlight(query.state.data?.cycle) ? RELEASES_LIVE_MS : RELEASES_IDLE_MS),
  });

/** A page of the cycles that ended, newest first. */
export const endedCyclesQuery = (api: ApiSource, project: string, offset = 0) =>
  queryOptions({
    queryKey: releaseKeys.cycles(project, offset),
    queryFn: ({ signal }) =>
      call(
        api().GET("/v1/projects/{project}/releases/cycles", {
          params: { path: { project }, query: { limit: CYCLES_PAGE, offset } },
          signal,
        }),
      ),
    placeholderData: keepPreviousData, // the page shown stays on screen while the next one loads
    refetchInterval: RELEASES_IDLE_MS,
  });

// Writes, each with the session's X-Evo-CSRF header.

export async function pauseRelease(api: ApiClient, project: string): Promise<ReleaseStatus> {
  const headers = await csrfHeaders(api);
  return call(api.POST("/v1/projects/{project}/releases/pause", { params: { path: { project } }, headers }));
}

export async function resumeRelease(api: ApiClient, project: string): Promise<ReleaseStatus> {
  const headers = await csrfHeaders(api);
  return call(api.POST("/v1/projects/{project}/releases/resume", { params: { path: { project } }, headers }));
}

export async function runReleaseCheck(api: ApiClient, project: string): Promise<ReleaseCheck> {
  const headers = await csrfHeaders(api);
  return call(api.POST("/v1/projects/{project}/releases/run", { params: { path: { project } }, headers }));
}
