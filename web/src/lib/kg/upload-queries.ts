import { queryOptions } from "@tanstack/react-query";

import { call } from "@/lib/api/client";
import type { components } from "@/lib/api/schema";
import type { ApiSource } from "@/lib/queries";

import { ACTIVE_REFRESH } from "./queries";
import type { KgUpload } from "./upload";

/**
 * The reads of the Upload data page: the project's uploads with their reports (GET /v1/kg/{project}/uploads), and its
 * kg_init runs (GET /v1/kg/{project}/init), to link an upload to the build and the Curator's first look that followed
 * it. Both ask again every few seconds while something still moves.
 */
export type KgInitRun = components["schemas"]["InitRun"];
export type KgInitRuns = components["schemas"]["InitRuns"];

export const UPLOADS_SHOWN = 20;
const INIT_SHOWN = 10;

export const uploadKeys = {
  uploads: (project: string) => ["kg", project, "uploads"] as const,
  init: (project: string) => ["kg", project, "init"] as const,
};

/** An upload still moving: being sent, being read, or read with its build not ended yet. */
export function uploadMoving(upload: KgUpload): boolean {
  if (upload.state === "uploading" || upload.state === "extracting") return true;
  return upload.state === "extracted" && (upload.build_status === "queued" || upload.build_status === "running");
}

export const kgUploadsQuery = (api: ApiSource, project: string) =>
  queryOptions({
    queryKey: uploadKeys.uploads(project),
    queryFn: ({ signal }) =>
      call(
        api().GET("/v1/kg/{project}/uploads", {
          params: { path: { project }, query: { limit: UPLOADS_SHOWN } },
          signal,
        }),
      ),
    refetchInterval: (query) => (query.state.data?.uploads.some(uploadMoving) ? ACTIVE_REFRESH : false),
  });

export const kgInitRunsQuery = (api: ApiSource, project: string, moving: boolean) =>
  queryOptions({
    queryKey: uploadKeys.init(project),
    queryFn: ({ signal }) =>
      call(api().GET("/v1/kg/{project}/init", { params: { path: { project }, query: { limit: INIT_SHOWN } }, signal })),
    refetchInterval: (query) => (moving || query.state.data?.active ? ACTIVE_REFRESH : false),
  });

/** The kg_init run that followed `upload`'s build, if one did. */
export function initRunFor(upload: KgUpload, runs: KgInitRun[] | undefined): KgInitRun | null {
  if (upload.build_id == null || !runs) return null;
  return runs.find((run) => run.build_id === upload.build_id) ?? null;
}
