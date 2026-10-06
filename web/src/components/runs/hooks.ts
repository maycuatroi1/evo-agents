"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useFormatter, useTranslations } from "next-intl";
import { useCallback } from "react";

import { type Notice, useWriteFailure, type WriteFailure } from "@/components/admin/notice";
import { workerKeys } from "@/components/workers/queries";
import { browserApi } from "@/lib/api/browser";
import { isApiError } from "@/lib/api/errors";
import { LOGIN_PATH } from "@/lib/config";
import { planKeys } from "@/lib/plan-queries";
import { whoamiQuery } from "@/lib/queries";

import {
  controlRun,
  dispatchPlanRun,
  dispatchRuns,
  type DispatchRequest,
  type PlanRunRequest,
  type Run,
  type RunControl,
  runKey,
  runKeys,
} from "./queries";
import type { RunViewer } from "./run-model";
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
 * A plan run dispatched from the browser, as `useDispatch`: nothing changes on screen until the hub answers, and either
 * way the project's runs, the plan and the workers are read again, so a 409 caused by another dispatch shows at once.
 */
export function useDispatchPlanRun(project: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: PlanRunRequest) => dispatchPlanRun(browserApi(), project, body),
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

/** "Queued plan run #14 of rollout.", for the notice a page shows after Run plan. */
export function usePlanRunNotice() {
  const t = useTranslations("runs.planRun");
  return useCallback((run: Run): Notice => ({ tone: "success", text: t("dispatched", { id: run.id, plan: run.plan_id }) }), [t]);
}

/**
 * The visitor as the run page needs them: their login, to tell the run's owner, and their role on the project (whoami's
 * grants), since approve and rerun also need the writer role. Null until whoami is read.
 */
export function useRunViewer(project: string): RunViewer | null {
  const { data } = useQuery(whoamiQuery(browserApi));
  if (!data) return null;
  const role = data.grants.find((grant) => grant.project === project)?.role ?? null;
  return {
    login: data.login,
    role: role === "reader" || role === "writer" || role === "admin" ? role : null,
    admin: Boolean(data.admin),
  };
}

/**
 * One of the owner's controls of a run, from the browser. Nothing changes on screen until the hub answers; success
 * and failure alike reload the run, the project's runs and the plan (an approve marks its step done, a cancel of a run
 * in review puts it back), so the page shows what the hub holds even after a 409 caused elsewhere. A 401 sends the
 * visitor to sign in.
 */
export function useRunControl(run: Pick<Run, "project" | "id" | "plan_id">) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (action: RunControl) => controlRun(browserApi(), run.project, run.id, action),
    onError: (error) => {
      if (isApiError(error) && error.kind === "unauthorized") window.location.assign(LOGIN_PATH);
    },
    onSettled: () =>
      Promise.all([
        queryClient.invalidateQueries({ queryKey: runKey(run.project, run.id) }),
        queryClient.invalidateQueries({ queryKey: runKeys.all(run.project) }),
        queryClient.invalidateQueries({ queryKey: planKeys.one(run.project, run.plan_id) }),
      ]),
  });
}

/** What to say when a run control or a message fails: the shared wording, with the API's own message kept as a detail. */
export function useRunFailure() {
  const failure = useWriteFailure();
  const t = useTranslations("runs.detail.errors");
  return useCallback(
    (error: unknown, conflict?: string): WriteFailure => {
      const base = failure(error, { 403: t("forbidden"), 404: t("notFound"), 409: conflict ?? t("conflict") });
      if (base.status === 409 || base.status === 413) {
        const message = isApiError(error) ? error.info.message : "";
        return { ...base, text: base.status === 413 ? t("full") : base.text, detail: message ? t("apiMessage", { message }) : null };
      }
      return base;
    },
    [failure, t],
  );
}
