"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useCallback } from "react";

import { useWriteFailure, type WriteFailure } from "@/components/admin/notice";
import { runKeys } from "@/components/runs/queries";
import { browserApi } from "@/lib/api/browser";
import { isApiError } from "@/lib/api/errors";
import { LOGIN_PATH } from "@/lib/config";
import { planKeys } from "@/lib/plan-queries";
import { whoamiQuery } from "@/lib/queries";

import {
  type AnswerRequest,
  answerDecision,
  type Decision,
  decisionKeys,
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
 * the hub returned, and its project's decisions, the member's notifications, the runs and the plan are read again (an
 * answer to a parked run ends it and queues the run that resumes it). After a failure the same reads show what the
 * hub holds, such as a decision answered meanwhile.
 */
export function useAnswerDecision(
  decision: Pick<Decision, "id" | "project" | "plan_id">,
  {
    onAnswered,
    onFailed,
  }: {
    /** Called even when the form that sent the answer is gone, which an answered decision replaces at once. */
    onAnswered?: (answered: Decision) => void;
    onFailed?: (error: unknown) => void;
  } = {},
) {
  const queryClient = useQueryClient();
  const keys = decisionKeys(decision.project);
  return useMutation({
    mutationFn: (body: AnswerRequest) => answerDecision(browserApi(), decision.project, decision.id, body),
    onError: (error) => {
      if (isApiError(error) && error.kind === "unauthorized") window.location.assign(LOGIN_PATH);
      else onFailed?.(error);
    },
    onSuccess: (answered) => {
      queryClient.setQueryData(keys.one(decision.id), answered);
      onAnswered?.(answered);
    },
    onSettled: () =>
      Promise.all([
        queryClient.invalidateQueries({ queryKey: keys.all }),
        queryClient.invalidateQueries({ queryKey: inboxKeys.all }),
        queryClient.invalidateQueries({ queryKey: runKeys.all(decision.project) }),
        queryClient.invalidateQueries({ queryKey: planKeys.one(decision.project, decision.plan_id) }),
      ]),
  });
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
