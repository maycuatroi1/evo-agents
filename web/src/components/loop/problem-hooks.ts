"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useState } from "react";

import { useWriteFailure, type WriteFailure } from "@/components/feedback/notice";
import { browserApi } from "@/lib/api/browser";
import { isApiError } from "@/lib/api/errors";
import { LOGIN_PATH } from "@/lib/config";
import { loopKeys } from "@/lib/loop-queries";
import { problemKeys } from "@/lib/problem-queries";
import { whoamiQuery } from "@/lib/queries";

export type ProblemViewer = { login: string; writer: boolean };

/** The visitor's login and whether they write the project's problems (the writer role or above); null until whoami is read. */
export function useProblemViewer(project: string): ProblemViewer | null {
  const { data } = useQuery(whoamiQuery(browserApi));
  if (!data) return null;
  const role = data.grants.find((grant) => grant.project === project)?.role ?? null;
  return { login: data.login, writer: role === "writer" || role === "admin" };
}

type Overrides = Parameters<ReturnType<typeof useWriteFailure>>[1];

/**
 * A write of a problem that a dialog runs: busy while it runs, the refusal kept for the dialog to say beside its
 * buttons (by status, in the words of `overrides`), then every read of the project's problems and goals again, whether
 * it worked or not, so a 409 shows what the other writer wrote. `done` gets what the write returned.
 */
export function useProblemWrite<T>(project: string) {
  const queryClient = useQueryClient();
  const failure = useWriteFailure();
  const [busy, setBusy] = useState(false);
  const [refused, setRefused] = useState<WriteFailure | null>(null);
  const run = useCallback(
    async (write: () => Promise<T>, done: (written: T) => void, overrides: Overrides = {}) => {
      setBusy(true);
      setRefused(null);
      try {
        done(await write());
      } catch (error) {
        if (isApiError(error) && error.kind === "unauthorized") window.location.assign(LOGIN_PATH);
        setRefused(failure(error, overrides));
      } finally {
        setBusy(false);
        await Promise.all([
          queryClient.invalidateQueries({ queryKey: problemKeys.all(project) }),
          queryClient.invalidateQueries({ queryKey: loopKeys.goalList(project) }),
        ]);
      }
    },
    [failure, project, queryClient],
  );
  const clear = useCallback(() => setRefused(null), []);
  return { busy, refused, run, clear };
}
