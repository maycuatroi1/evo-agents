import { useMutation, useQueryClient } from "@tanstack/react-query";

import { type ApiClient, call } from "@/lib/api/client";
import { browserApi } from "@/lib/api/browser";
import { csrfHeaders } from "@/lib/api/csrf";
import { isApiError } from "@/lib/api/errors";
import { LOGIN_PATH } from "@/lib/config";
import { planKeys } from "@/lib/plan-queries";

import { type Run, runKeys } from "./queries";

/** Run plan on a Curator's plan: the hub queues its Builder now, beside the night shift's other runs. */
export async function buildCuratorPlan(api: ApiClient, project: string, planId: string): Promise<Run> {
  const headers = await csrfHeaders(api);
  return call(api.POST("/v1/projects/{project}/curator/plans/{plan_id}/build", { params: { path: { project, plan_id: planId } }, headers }));
}

/** The Builder of the Curator's plan `planId` queued now; the project's runs and the plan are read again. */
export function useBuildCuratorPlan(project: string, planId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => buildCuratorPlan(browserApi(), project, planId),
    onError: (error) => {
      if (isApiError(error) && error.kind === "unauthorized") window.location.assign(LOGIN_PATH);
    },
    onSettled: () =>
      Promise.all([
        queryClient.invalidateQueries({ queryKey: runKeys.all(project) }),
        queryClient.invalidateQueries({ queryKey: planKeys.one(project, planId) }),
      ]),
  });
}
