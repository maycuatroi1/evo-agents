"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { Route } from "next";
import { useTranslations } from "next-intl";
import { useCallback } from "react";

import { useWriteFailure, type WriteFailure } from "@/components/admin/notice";
import { runKeys, runQuery, runTerminalHref } from "@/components/runs/queries";
import { isOpenState, terminalAccess } from "@/components/runs/terminal-model";
import { workerQuery } from "@/components/workers/queries";
import { browserApi } from "@/lib/api/browser";
import { isApiError } from "@/lib/api/errors";
import { LOGIN_PATH } from "@/lib/config";
import { planKeys } from "@/lib/plan-queries";
import { overviewQuery, queryKeys, whoamiQuery } from "@/lib/queries";

import {
  type AnswerRequest,
  answerDecision,
  type Decision,
  decisionKeys,
  decisionQuery,
  inboxKeys,
  markNotificationsRead,
} from "./queries";

/** The signed-in member's login, and the projects they hold a grant on (whoami, read by the app's layout). */
export function useInboxViewer(): { login: string | null; projects: string[] } {
  const { data } = useQuery(whoamiQuery(browserApi));
  return { login: data?.login ?? null, projects: data ? data.grants.map((grant) => grant.project) : [] };
}

/**
 * Mark notifications read, by id or all of them. Either way the bell and the Inbox are read again, so a request that
 * failed shows what the hub holds. A 401 sends the visitor to sign in.
 */
export function useMarkRead() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (target: { ids: number[] } | { all: true }) => markNotificationsRead(browserApi(), target),
    onError: (error) => {
      if (isApiError(error) && error.kind === "unauthorized") window.location.assign(LOGIN_PATH);
    },
    onSettled: () => queryClient.invalidateQueries({ queryKey: inboxKeys.all }),
  });
}

/**
 * Answer a decision from the browser. Nothing changes on screen until the hub answers; then the decision is the one
 * the hub returned, and its project's decisions, the member's notifications and overview, the runs and the plan are
 * read again (an answer to a parked run ends it and queues the run that resumes it). A 409 means the decision was
 * answered or closed meanwhile: the decision is read again at once and handed to `onConflict`, so the card shows it as
 * the hub holds it beside the toast that says why. After any failure the same reads show what the hub holds.
 */
export function useAnswerDecision(
  decision: Pick<Decision, "id" | "project" | "plan_id">,
  {
    onAnswered,
    onFailed,
    onConflict,
  }: {
    /** Called even when the form that sent the answer is gone, which an answered decision replaces at once. */
    onAnswered?: (answered: Decision) => void;
    onFailed?: (error: unknown) => void;
    /** The decision as the hub holds it after a 409, read again. */
    onConflict?: (fresh: Decision) => void;
  } = {},
) {
  const queryClient = useQueryClient();
  const keys = decisionKeys(decision.project);
  return useMutation({
    mutationFn: (body: AnswerRequest) => answerDecision(browserApi(), decision.project, decision.id, body),
    onError: async (error) => {
      if (isApiError(error) && error.kind === "unauthorized") {
        window.location.assign(LOGIN_PATH);
        return;
      }
      onFailed?.(error);
      if (isApiError(error) && error.status === 409) {
        const fresh = await queryClient.fetchQuery({ ...decisionQuery(browserApi, decision.project, decision.id), staleTime: 0 }).catch(() => null);
        if (fresh) onConflict?.(fresh);
      }
    },
    onSuccess: (answered) => {
      queryClient.setQueryData(keys.one(decision.id), answered);
      onAnswered?.(answered);
    },
    onSettled: () =>
      Promise.all([
        queryClient.invalidateQueries({ queryKey: keys.all }),
        queryClient.invalidateQueries({ queryKey: inboxKeys.all }),
        queryClient.invalidateQueries({ queryKey: queryKeys.overview }),
        queryClient.invalidateQueries({ queryKey: runKeys.all(decision.project) }),
        queryClient.invalidateQueries({ queryKey: planKeys.one(decision.project, decision.plan_id) }),
      ]),
  });
}

/**
 * When the run of an open decision parks, as the hub says it in GET /v1/me/overview (the wait is the hub's setting,
 * EVO_HUB_DECISION_WAIT_SECONDS). Read only while the run waits or is parked, again each time it starts to; null while
 * its agent works, and for a decision past the 20 the overview lists. `known` is the value a caller already holds,
 * such as the Home's own overview.
 */
export function useParksAt(decision: Pick<Decision, "id" | "project" | "state" | "run_state">, known?: string | null): string | null {
  const waits = decision.state === "open" && (decision.run_state === "waiting" || decision.run_state === "parked");
  const { data } = useQuery({
    ...overviewQuery(browserApi),
    enabled: waits && known === undefined,
    staleTime: 0, // read when the run starts to wait, since it was null while the agent worked
    select: (overview) => overview.open_decisions.find((item) => item.id === decision.id && item.project === decision.project)?.parks_at ?? null,
  });
  if (!waits) return null;
  return known !== undefined ? known : (data ?? null);
}

/**
 * Where the owner of a decision's run takes it over in the terminal: the run's page on its Terminal tab, offered while
 * they may open the run's terminal now (their run on a worker of theirs that allows the web terminal, in a state the
 * hub opens a terminal in). The run and its worker are read only for the owner, and only in such a state.
 */
export function useDecisionTerminalHref(decision: Pick<Decision, "project" | "run_id" | "run_state" | "owner" | "state">): Route | null {
  const { login } = useInboxViewer();
  const owner = login !== null && login === decision.owner;
  const candidate = owner && decision.state === "open" && isOpenState(decision.run_state);
  const run = useQuery({ ...runQuery(browserApi, decision.project, decision.run_id), enabled: candidate });
  const workerId = run.data?.worker_id ?? null;
  const worker = useQuery({
    ...workerQuery(browserApi, workerId ?? 0),
    enabled: candidate && workerId !== null,
    refetchInterval: false, // its owner and whether it allows the terminal are fixed when it registers
  });
  if (!candidate || !run.data) return null;
  const access = terminalAccess(run.data, login === null ? null : { login }, worker.data);
  return access?.open ? runTerminalHref(decision.project, decision.run_id) : null;
}

/** What to say when an answer fails: 403, 404 and 409 in the decision's own words, the hub's message kept as a detail. */
export function useAnswerFailure() {
  const failure = useWriteFailure();
  const t = useTranslations("inbox.errors");
  return useCallback(
    (error: unknown, decision: Pick<Decision, "owner" | "run_id" | "project">): WriteFailure => {
      const base = failure(error, {
        403: t("forbidden", { owner: decision.owner, run: decision.run_id, project: decision.project }),
        404: t("notFound"),
        409: t("conflict"),
      });
      if (base.status === 403 || base.status === 409) {
        const message = isApiError(error) ? error.info.message : "";
        return { ...base, detail: message ? t("apiMessage", { message }) : null };
      }
      return base;
    },
    [failure, t],
  );
}

/** What to say when marking notifications read fails. */
export function useReadFailure() {
  const failure = useWriteFailure();
  const t = useTranslations("inbox.errors");
  return useCallback((error: unknown): WriteFailure => failure(error, { 403: t("readForbidden"), 404: t("readGone"), 409: t("readGone") }), [failure, t]);
}
