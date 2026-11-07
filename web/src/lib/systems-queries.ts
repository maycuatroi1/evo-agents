import { queryOptions } from "@tanstack/react-query";
import type { Route } from "next";

import { type ApiClient, call } from "@/lib/api/client";
import { csrfHeaders } from "@/lib/api/csrf";
import type { ApiSource } from "@/lib/queries";

import { type Spec, specBody, type SystemChecked, type SystemWritten } from "./systems";

/**
 * The reads and writes of a project's agent systems (GET, PUT /v1/projects/{p}/systems and below). Every member reads
 * them and may check a draft (POST .../validate); only an admin of the project writes one. A write names the revision
 * it changes (`if_revision`): another one written since is 409 `revision_conflict`, a spec with an error is 422 with
 * the problems as the error's detail.
 */
export const systemKeys = {
  all: (project: string) => ["projects", project, "systems"] as const,
  list: (project: string) => ["projects", project, "systems", "list"] as const,
  one: (project: string, slug: string) => ["projects", project, "systems", "one", slug] as const,
  traces: (project: string, slug: string) => ["projects", project, "systems", "one", slug, "traces"] as const,
  trace: (project: string, slug: string, id: number) => ["projects", project, "systems", "one", slug, "traces", id] as const,
  metrics: (project: string, slug: string) => ["projects", project, "systems", "one", slug, "metrics"] as const,
};

/** The recent traces a system's pages hold: the test sheet's and Observe's list. */
export const TRACE_LIST_LIMIT = 20;

export const REVISION_CONFLICT = "revision_conflict";

/** The project's systems, by slug. */
export const systemsQuery = (api: ApiSource, project: string) =>
  queryOptions({
    queryKey: systemKeys.list(project),
    queryFn: ({ signal }) => call(api().GET("/v1/projects/{project}/systems", { params: { path: { project } }, signal })),
    staleTime: 15_000,
  });

/** One system at its latest revision, with its pins and the problems the hub finds in it now. */
export const systemQuery = (api: ApiSource, project: string, slug: string) =>
  queryOptions({
    queryKey: systemKeys.one(project, slug),
    queryFn: ({ signal }) =>
      call(api().GET("/v1/projects/{project}/systems/{slug}", { params: { path: { project, slug } }, signal })),
  });

/** The latest traces of a system, the newest first. */
export const systemTracesQuery = (api: ApiSource, project: string, slug: string, limit = TRACE_LIST_LIMIT) =>
  queryOptions({
    queryKey: systemKeys.traces(project, slug),
    queryFn: ({ signal }) =>
      call(
        api().GET("/v1/projects/{project}/systems/{slug}/traces", { params: { path: { project, slug }, query: { limit } }, signal }),
      ),
  });

/** One trace with its spans. */
export const systemTraceQuery = (api: ApiSource, project: string, slug: string, id: number) =>
  queryOptions({
    queryKey: systemKeys.trace(project, slug, id),
    queryFn: ({ signal }) =>
      call(
        api().GET("/v1/projects/{project}/systems/{slug}/traces/{trace_id}", {
          params: { path: { project, slug, trace_id: id } },
          signal,
        }),
      ),
  });

/** The numbers of each node and edge of a system over its last traces. */
export const systemMetricsQuery = (api: ApiSource, project: string, slug: string) =>
  queryOptions({
    queryKey: systemKeys.metrics(project, slug),
    queryFn: ({ signal }) =>
      call(api().GET("/v1/projects/{project}/systems/{slug}/metrics", { params: { path: { project, slug } }, signal })),
  });

/** The stream of a system's traces as they move, from `after` (an ISO time the hub wrote) or from now. */
export function systemTraceStreamPath(project: string, slug: string, after: string | null): string {
  const path = `/v1/projects/${encodeURIComponent(project)}/systems/${encodeURIComponent(slug)}/traces/stream`;
  return after ? `${path}?after=${encodeURIComponent(after)}` : path;
}

export function systemsHref(project: string): Route {
  return `/p/${encodeURIComponent(project)}/systems` as Route;
}

export function systemHref(project: string, slug: string): Route {
  return `${systemsHref(project)}/${encodeURIComponent(slug)}` as Route;
}

/** The system's page in Observe mode. */
export function systemObserveHref(project: string, slug: string): Route {
  return `${systemHref(project, slug)}?mode=observe` as Route;
}

/** The page of one trace of a system, with its waterfall. */
export function systemTraceHref(project: string, slug: string, id: number): Route {
  return `${systemHref(project, slug)}/traces/${id}` as Route;
}

/** What the hub finds in a draft, without writing it: the problems as a save would meet them. */
export async function validateSystem(api: ApiClient, project: string, spec: Spec, slug: string | null, signal?: AbortSignal): Promise<SystemChecked> {
  const headers = await csrfHeaders(api);
  return call(
    api.POST("/v1/projects/{project}/systems/validate", {
      params: { path: { project } },
      body: { spec: specBody(spec), ...(slug ? { slug } : {}) },
      headers,
      signal,
    }),
  );
}

/** Write `spec` as the system's next revision, or create it when `ifRevision` is null. */
export async function putSystem(api: ApiClient, project: string, slug: string, spec: Spec, ifRevision: number | null): Promise<SystemWritten> {
  const headers = await csrfHeaders(api);
  return call(
    api.PUT("/v1/projects/{project}/systems/{slug}", {
      params: { path: { project, slug } },
      body: { spec: specBody(spec), ...(ifRevision ? { if_revision: ifRevision } : {}) },
      headers,
    }),
  );
}
