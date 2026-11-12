"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useState } from "react";

import { useWriteFailure, type WriteFailure } from "@/components/feedback/notice";
import { browserApi } from "@/lib/api/browser";
import { isApiError } from "@/lib/api/errors";
import { LOGIN_PATH } from "@/lib/config";
import { whoamiQuery } from "@/lib/queries";

import { cronKeys } from "./queries";

/** Whether the visitor makes cron jobs in the project (the writer role or above); false until whoami is read. */
export function useCronWriter(project: string): boolean {
  const { data } = useQuery(whoamiQuery(browserApi));
  const role = data?.grants.find((grant) => grant.project === project)?.role ?? null;
  return role === "writer" || role === "admin";
}

type Overrides = Parameters<ReturnType<typeof useWriteFailure>>[1];

/**
 * A write of a cron job that a dialog or a menu runs: busy while it runs, the refusal kept to say beside the buttons
 * (by status, in the words of `overrides`), then the project's jobs and fires read again whether it worked or not, so
 * a 409 shows the job as the hub holds it now. `done` gets what the write returned.
 */
export function useCronWrite<T>(project: string) {
  const queryClient = useQueryClient();
  const failure = useWriteFailure();
  const [busy, setBusy] = useState(false);
  const [refused, setRefused] = useState<WriteFailure | null>(null);
  const run = useCallback(
    async (write: () => Promise<T>, done: (written: T) => void, overrides: Overrides = {}): Promise<WriteFailure | null> => {
      setBusy(true);
      setRefused(null);
      let failed: WriteFailure | null = null;
      try {
        done(await write());
      } catch (error) {
        if (isApiError(error) && error.kind === "unauthorized") window.location.assign(LOGIN_PATH);
        failed = failure(error, overrides);
        setRefused(failed);
      } finally {
        setBusy(false);
        await queryClient.invalidateQueries({ queryKey: cronKeys.all(project) });
      }
      return failed;
    },
    [failure, project, queryClient],
  );
  const clear = useCallback(() => setRefused(null), []);
  return { busy, refused, run, clear };
}
