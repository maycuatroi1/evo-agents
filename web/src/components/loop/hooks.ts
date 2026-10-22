"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { browserApi } from "@/lib/api/browser";
import { isApiError } from "@/lib/api/errors";
import { LOGIN_PATH } from "@/lib/config";
import { loopKeys, resumeLoop, setLoopEnabled } from "@/lib/loop-queries";
import { mergeKeys } from "@/lib/merge-queue";
import { whoamiQuery } from "@/lib/queries";

export type LoopViewer = { login: string; admin: boolean };

/** The visitor's login and whether they are an admin of `project`; null until whoami is read. */
export function useLoopViewer(project: string): LoopViewer | null {
  const { data } = useQuery(whoamiQuery(browserApi));
  if (!data) return null;
  const role = data.grants.find((grant) => grant.project === project)?.role ?? null;
  return { login: data.login, admin: role === "admin" };
}

function signInAgainOn401(error: unknown): void {
  if (isApiError(error) && error.kind === "unauthorized") window.location.assign(LOGIN_PATH);
}

/**
 * The Loop turned on or off by an admin of the project, as the next revision of its policy with `note`; then every read
 * of the Loop and of the policy again. A 409 means another admin wrote the policy since it was read.
 */
export function useLoopSwitch(project: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ enabled, note }: { enabled: boolean; note: string }) => setLoopEnabled(browserApi(), project, enabled, note),
    onSuccess: () =>
      Promise.all([
        queryClient.invalidateQueries({ queryKey: loopKeys.all(project) }),
        queryClient.invalidateQueries({ queryKey: mergeKeys.all(project) }),
      ]),
    onError: signInAgainOn401,
  });
}

/** The breaker of the Loop reset by an admin: the Loop reads its events again from its next tick. */
export function useLoopResume(project: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => resumeLoop(browserApi(), project),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: loopKeys.all(project) }),
    onError: signInAgainOn401,
  });
}
