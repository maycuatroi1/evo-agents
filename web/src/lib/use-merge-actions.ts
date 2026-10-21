"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";

import { browserApi } from "@/lib/api/browser";
import { isApiError } from "@/lib/api/errors";
import { LOGIN_PATH } from "@/lib/config";
import { answerSample, approveRelease, askRevert, mergeKeys, resumeRepo, type SampleResult } from "@/lib/merge-queue";

/** The member's notifications (`inboxKeys.all` of the Inbox): an answer marks the queue's notices read on the hub. */
const NOTIFICATIONS = ["me", "notifications"] as const;

function unauthorized(error: unknown): void {
  if (isApiError(error) && error.kind === "unauthorized") window.location.assign(LOGIN_PATH);
}

/**
 * What an admin of the project answers to the merge queue, from its pages or from the Inbox: an audit sample, the PROD
 * release of a merge, the revert of a merge, the reset of a repo's breaker. Each reads the project's queue and the
 * member's notifications again once the hub answered, whatever it answered.
 */
function useMergeMutation<TVariables, TResult>(project: string, write: (variables: TVariables) => Promise<TResult>) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: write,
    onError: unauthorized,
    onSettled: () =>
      Promise.all([
        queryClient.invalidateQueries({ queryKey: mergeKeys.all(project) }),
        queryClient.invalidateQueries({ queryKey: NOTIFICATIONS }),
      ]),
  });
}

export function useAnswerSample(project: string) {
  return useMergeMutation(project, ({ sampleId, result, note }: { sampleId: number; result: SampleResult; note: string | null }) =>
    answerSample(browserApi(), project, sampleId, result, note),
  );
}

export function useApproveRelease(project: string) {
  return useMergeMutation(project, (queueId: number) => approveRelease(browserApi(), project, queueId));
}

export function useAskRevert(project: string) {
  return useMergeMutation(project, (queueId: number) => askRevert(browserApi(), project, queueId));
}

export function useResumeRepo(project: string) {
  return useMergeMutation(project, (repo: string) => resumeRepo(browserApi(), project, repo));
}
