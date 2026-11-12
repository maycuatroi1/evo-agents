import { queryOptions } from "@tanstack/react-query";
import type { Route } from "next";

import { projectHref } from "@/components/shell/nav";
import { type ApiClient, call } from "@/lib/api/client";
import { csrfHeaders } from "@/lib/api/csrf";
import type { components } from "@/lib/api/schema";
import type { ApiSource } from "@/lib/queries";

/**
 * The reads and writes of a project's cron jobs (GET, PUT, POST, DELETE /v1/projects/{p}/cron-jobs and below; docs/hub.md,
 * Cron jobs). Every member of the project reads the jobs and their fires; a writer makes jobs and changes their own
 * (`can_change`); the owner or an admin of the project pauses, resumes, fires and deletes one (`can_manage`).
 */
type Schemas = components["schemas"];
export type CronJob = Schemas["CronJobOut"];
export type CronFire = Schemas["CronFireOut"];
export type CronJobPut = Schemas["CronJobPut"];
export type CronKind = CronJob["kind"];
export type FireOutcome = NonNullable<CronFire["outcome"]>;

export const CRON_KINDS = ["agent", "plan", "command"] as const satisfies readonly CronKind[];
export const FIRE_OUTCOMES = ["started", "busy", "missed", "refused"] as const satisfies readonly FireOutcome[];
/** The page and the history refresh this often, so a fire shows within a minute of its time. */
export const CRON_REFRESH_MS = 30_000;
export const FIRES_SHOWN = 100;

export const cronKeys = {
  all: (project: string) => ["projects", project, "cron-jobs"] as const,
  list: (project: string) => ["projects", project, "cron-jobs", "list"] as const,
  fires: (project: string, name: string) => ["projects", project, "cron-jobs", "fires", name] as const,
};

/** The project's cron jobs, by name, each with its next fire, its last fire and what its runs cost in 24 hours. */
export const cronJobsQuery = (api: ApiSource, project: string) =>
  queryOptions({
    queryKey: cronKeys.list(project),
    queryFn: ({ signal }) => call(api().GET("/v1/projects/{project}/cron-jobs", { params: { path: { project } }, signal })),
    refetchInterval: CRON_REFRESH_MS,
  });

/** The fires of one job, the latest due first. */
export const cronFiresQuery = (api: ApiSource, project: string, name: string) =>
  queryOptions({
    queryKey: cronKeys.fires(project, name),
    queryFn: ({ signal }) =>
      call(
        api().GET("/v1/projects/{project}/cron-jobs/{name}/fires", {
          params: { path: { project, name }, query: { limit: FIRES_SHOWN } },
          signal,
        }),
      ),
    refetchInterval: CRON_REFRESH_MS,
  });

export function cronJobsHref(project: string): Route {
  return projectHref(project, "cron-jobs");
}

/** A run's page; the runs folder is another feature, so the path is built here. */
export function cronRunHref(project: string, run: number): Route {
  return projectHref(project, `runs/${run}`);
}

/** Create a job (`if_revision` 0 or none) or store what it starts as its next revision (409 when another write came first). */
export async function putCronJob(api: ApiClient, project: string, name: string, body: CronJobPut) {
  const headers = await csrfHeaders(api);
  return call(api.PUT("/v1/projects/{project}/cron-jobs/{name}", { params: { path: { project, name } }, body, headers }));
}

export async function pauseCronJob(api: ApiClient, project: string, name: string, reason: string | null) {
  const headers = await csrfHeaders(api);
  return call(
    api.POST("/v1/projects/{project}/cron-jobs/{name}/pause", { params: { path: { project, name } }, body: { reason }, headers }),
  );
}

export async function resumeCronJob(api: ApiClient, project: string, name: string) {
  const headers = await csrfHeaders(api);
  return call(api.POST("/v1/projects/{project}/cron-jobs/{name}/resume", { params: { path: { project, name } }, headers }));
}

/** Fire a job now: the hub records a fire due at this moment and starts its run within a minute. */
export async function runCronJob(api: ApiClient, project: string, name: string) {
  const headers = await csrfHeaders(api);
  return call(api.POST("/v1/projects/{project}/cron-jobs/{name}/run", { params: { path: { project, name } }, headers }));
}

/** Delete a job with its fires; the runs it started stay. */
export async function deleteCronJob(api: ApiClient, project: string, name: string) {
  const headers = await csrfHeaders(api);
  return call(api.DELETE("/v1/projects/{project}/cron-jobs/{name}", { params: { path: { project, name } }, headers }));
}
