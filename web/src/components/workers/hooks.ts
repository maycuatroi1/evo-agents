"use client";

import { type QueryKey, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useCallback, useEffect } from "react";

import { useWriteFailure, type WriteFailure } from "@/components/admin/notice";
import { browserApi } from "@/lib/api/browser";
import type { ApiClient } from "@/lib/api/client";
import { isApiError } from "@/lib/api/errors";
import { LOGIN_PATH } from "@/lib/config";
import { queryKeys } from "@/lib/queries";

import { recordHeartbeats } from "./heartbeats";
import { parseWorkerId, type Worker, workerKeys, workerQuery } from "./queries";

/**
 * A write to the workers API from the browser. Nothing changes on screen until the hub answers; success and failure
 * alike reload every worker query, so the page shows what the hub holds even after a 404 or 409 caused elsewhere
 * (the worker was revoked from another tab, or by a hub admin). A 401 sends the visitor to sign in.
 */
export function useWorkerWrite<TArgs, TResult>(write: (api: ApiClient, args: TArgs) => Promise<TResult>) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (args: TArgs) => write(browserApi(), args),
    onError: (error) => {
      if (isApiError(error) && error.kind === "unauthorized") window.location.assign(LOGIN_PATH);
    },
    onSettled: () =>
      Promise.all([
        queryClient.invalidateQueries({ queryKey: workerKeys.all }),
        queryClient.invalidateQueries({ queryKey: queryKeys.whoami }),
      ]),
  });
}

type Overrides = Partial<Record<403 | 404 | 409 | 422 | 503, string>>;

/** What to say when a workers write fails: the shared wording, with the workers' own texts for 403 to 503. */
export function useWorkerFailure() {
  const failure = useWriteFailure();
  const t = useTranslations("workers.errors");
  return useCallback(
    (error: unknown, overrides: Overrides = {}): WriteFailure => {
      const status = isApiError(error) ? error.status : 0;
      if (status === 503) {
        const message = isApiError(error) ? error.info.message : "";
        return {
          text: overrides[503] ?? t("unavailable"),
          detail: message ? t("apiMessage", { message }) : null,
          requestId: isApiError(error) ? error.info.requestId : null,
          status,
        };
      }
      return failure(error, { 403: t("forbidden"), 404: t("notFound"), 409: t("conflict"), ...overrides });
    },
    [failure, t],
  );
}

/**
 * Data the server prefetched never went through the browser's query function, so it is recorded as an observation
 * once, when the page mounts, with the time the server read it.
 */
export function useRecordPrefetched(queryKey: QueryKey, pick: (data: unknown) => Worker[]) {
  const queryClient = useQueryClient();
  useEffect(() => {
    const state = queryClient.getQueryState(queryKey);
    if (state?.data !== undefined) recordHeartbeats(pick(state.data), state.dataUpdatedAt);
    // Once per page: later answers are recorded by the query function itself.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [queryClient]);
}

/**
 * The name of the worker a path part names, once its page has read it: the breadcrumb shows the name instead of
 * the id. It reads the cache only and never asks the hub itself.
 */
export function useWorkerName(part: string | undefined): string | null {
  const id = part ? parseWorkerId(part) : null;
  const { data } = useQuery({ ...workerQuery(browserApi, id ?? 0), enabled: false });
  return id !== null && data ? data.name : null;
}
