"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useFormatter, useTranslations } from "next-intl";

import type { Notice } from "@/components/admin/notice";
import { workerKeys } from "@/components/workers/queries";
import { browserApi } from "@/lib/api/browser";
import { isApiError } from "@/lib/api/errors";
import { LOGIN_PATH } from "@/lib/config";
import { planKeys } from "@/lib/plan-queries";
import { whoamiQuery } from "@/lib/queries";

import { dispatchRuns, type DispatchRequest, type Run, runKeys } from "./queries";
import type { Viewer } from "./runs-table";

/**
 * A dispatch from the browser. Nothing changes on screen until the hub answers; success and failure alike reload the
 * project's runs, the plan (and with it the readiness of its steps) and the workers, so the page shows what the hub
 * holds even after a 409 caused by another dispatch. A 401 sends the visitor to sign in.
 */
export function useDispatch(project: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: DispatchRequest) => dispatchRuns(browserApi(), project, body),
    onError: (error) => {
      if (isApiError(error) && error.kind === "unauthorized") window.location.assign(LOGIN_PATH);
    },
    onSettled: (_data, _error, body) =>
      Promise.all([
        queryClient.invalidateQueries({ queryKey: runKeys.all(project) }),
        queryClient.invalidateQueries({ queryKey: planKeys.one(project, body.plan_id) }),
        queryClient.invalidateQueries({ queryKey: workerKeys.all }),
      ]),
  });
}

/**
 * Whether the visitor may dispatch in `project`: a grant on it with the writer or admin role, as the session's whoami
 * (read by the app's layout) lists it. The hub admin role alone does not dispatch. The API decides again on every
 * dispatch; this only decides whether the button is offered.
 */
export function useCanDispatch(project: string): boolean {
  const { data } = useQuery(whoamiQuery(browserApi));
  const role = data?.grants.find((grant) => grant.project === project)?.role;
  return role === "writer" || role === "admin";
}

/** The signed-in member, for links to the workers they may open. */
export function useViewer(): Viewer | null {
  const { data } = useQuery(whoamiQuery(browserApi));
  return data ? { login: data.login, admin: Boolean(data.admin) } : null;
}

/** "Queued 2 runs: #12 and #13.", for the notice a page shows after a dispatch. */
export function useDispatchedNotice() {
  const t = useTranslations("runs");
  const format = useFormatter();
  return (runs: Run[]): Notice => ({
    tone: "success",
    text: t("dispatched", { count: runs.length, ids: format.list(runs.map((run) => `#${run.id}`), { type: "conjunction" }) }),
  });
}
