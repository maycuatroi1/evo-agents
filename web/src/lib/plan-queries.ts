import { queryOptions } from "@tanstack/react-query";

import { call } from "@/lib/api/client";
import type { ApiSource } from "@/lib/queries";

/**
 * The reads of a project's plans, as TanStack Query options shared by the server (prefetch) and the browser (see
 * `src/lib/queries.ts`). Every one is a GET: the web never writes a plan.
 */
export const planKeys = {
  all: (project: string) => ["projects", project, "plans"] as const,
  one: (project: string, plan: string) => ["projects", project, "plans", plan] as const,
  revisions: (project: string, plan: string) => ["projects", project, "plans", plan, "revisions"] as const,
  diff: (project: string, plan: string, from: number, to: number) =>
    ["projects", project, "plans", plan, "diff", from, to] as const,
};

export const plansQuery = (api: ApiSource, project: string) =>
  queryOptions({
    queryKey: planKeys.all(project),
    queryFn: ({ signal }) => call(api().GET("/v1/projects/{project}/plans", { params: { path: { project } }, signal })),
  });

export const planQuery = (api: ApiSource, project: string, plan: string) =>
  queryOptions({
    queryKey: planKeys.one(project, plan),
    queryFn: ({ signal }) =>
      call(api().GET("/v1/projects/{project}/plans/{plan_id}", { params: { path: { project, plan_id: plan } }, signal })),
  });

export const planRevisionsQuery = (api: ApiSource, project: string, plan: string) =>
  queryOptions({
    queryKey: planKeys.revisions(project, plan),
    queryFn: ({ signal }) =>
      call(
        api().GET("/v1/projects/{project}/plans/{plan_id}/revisions", {
          params: { path: { project, plan_id: plan } },
          signal,
        }),
      ),
  });

export const planDiffQuery = (api: ApiSource, project: string, plan: string, from: number, to: number) =>
  queryOptions({
    queryKey: planKeys.diff(project, plan, from, to),
    queryFn: ({ signal }) =>
      call(
        api().GET("/v1/projects/{project}/plans/{plan_id}/diff", {
          params: { path: { project, plan_id: plan }, query: { from, to } },
          signal,
        }),
      ),
    staleTime: Infinity, // two revisions never change, so neither does their diff
  });
