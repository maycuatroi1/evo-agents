"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useCallback } from "react";

import { useWriteFailure, type WriteFailure } from "@/components/feedback/notice";
import { browserApi } from "@/lib/api/browser";
import { isApiError } from "@/lib/api/errors";
import { LOGIN_PATH } from "@/lib/config";
import { queryKeys, whoamiQuery } from "@/lib/queries";

import { releaseRights } from "./model";
import { pauseRelease, releaseKeys, resumeRelease, runReleaseCheck } from "./queries";

/** What the visitor may do on the Releases page of `project`, from their role on it (whoami, which the layout reads). */
export function useReleaseRights(project: string): { admin: boolean } {
  const { data } = useQuery(whoamiQuery(browserApi));
  const role = data?.grants.find((grant) => grant.project === project)?.role ?? null;
  return releaseRights(data ? { role } : null);
}

function unauthorized(error: unknown): boolean {
  if (isApiError(error) && error.kind === "unauthorized") {
    window.location.assign(LOGIN_PATH);
    return true;
  }
  return false;
}

/** After a write, everything the page reads of the project's release is read again, and its runs and Home too. */
function useSettled(project: string) {
  const queryClient = useQueryClient();
  return () =>
    Promise.all([
      queryClient.invalidateQueries({ queryKey: releaseKeys.all(project) }),
      queryClient.invalidateQueries({ queryKey: queryKeys.overview }),
      queryClient.invalidateQueries({ queryKey: ["projects", project, "runs"] }),
    ]);
}

/**
 * Pause or resume the release schedule. The hub answers with where the release stands, which replaces what the page
 * shows; a pause cancels the release runs still queued and closes a cycle left idle.
 */
export function usePauseRelease(project: string) {
  const queryClient = useQueryClient();
  const settled = useSettled(project);
  return useMutation({
    mutationFn: (pause: boolean) => (pause ? pauseRelease(browserApi(), project) : resumeRelease(browserApi(), project)),
    onError: unauthorized,
    onSuccess: (status) => queryClient.setQueryData(releaseKeys.status(project), status),
    onSettled: settled,
  });
}

/** Run the check now, through the gates of the daily one; the hub answers with the check it recorded. */
export function useRunReleaseCheck(project: string) {
  const settled = useSettled(project);
  return useMutation({
    mutationFn: () => runReleaseCheck(browserApi(), project),
    onError: unauthorized,
    onSettled: settled,
  });
}

/** What to say when a write of the release fails: 403, 404 and 409 in the page's own words, the hub's message kept. */
export function useReleaseFailure() {
  const failure = useWriteFailure();
  const t = useTranslations("releases.errors");
  return useCallback(
    (error: unknown): WriteFailure => {
      const base = failure(error, { 403: t("forbidden"), 404: t("notFound"), 409: t("conflict") });
      if (base.status === 403 || base.status === 409) {
        const message = isApiError(error) ? error.info.message : "";
        return { ...base, detail: message ? t("apiMessage", { message }) : null };
      }
      return base;
    },
    [failure, t],
  );
}
