import { queryOptions } from "@tanstack/react-query";

import { type ApiClient, call } from "@/lib/api/client";
import { csrfHeaders } from "@/lib/api/csrf";
import type { components } from "@/lib/api/schema";
import type { RunOutput } from "@/lib/outputs";
import type { ApiSource } from "@/lib/queries";

import { runKey } from "./queries";

/**
 * A run's outputs on its page (docs/hub.md, Run outputs): its readers list them, oldest first, with how many the run
 * holds, their bytes and whether the project keeps images of tools' results (GET .../runs/{id}/outputs); the run's
 * dispatcher or an admin of the project deletes one (DELETE .../outputs/{oid}). Below the run's own key, so reading
 * the run again (a move the log tells) reads its outputs too.
 */
export type RunOutputs = components["schemas"]["RunOutputs"];

/** While the run is active its outputs are read this often, besides each time the log says the hub recorded some. */
export const OUTPUTS_REFRESH_MS = 15_000;

export const outputsKey = (project: string, runId: number) => [...runKey(project, runId), "outputs"] as const;

export const runOutputsQuery = (api: ApiSource, project: string, runId: number, active: boolean) =>
  queryOptions({
    queryKey: outputsKey(project, runId),
    queryFn: ({ signal }) =>
      call(api().GET("/v1/projects/{project}/runs/{run_id}/outputs", { params: { path: { project, run_id: runId } }, signal })),
    refetchInterval: active ? OUTPUTS_REFRESH_MS : false,
  });

export async function deleteRunOutput(api: ApiClient, project: string, runId: number, outputId: number): Promise<RunOutput> {
  const headers = await csrfHeaders(api);
  return call(
    api.DELETE("/v1/projects/{project}/runs/{run_id}/outputs/{output_id}", {
      params: { path: { project, run_id: runId, output_id: outputId } },
      headers,
    }),
  );
}
