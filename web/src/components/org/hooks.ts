"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useCallback, useState, useSyncExternalStore } from "react";

import { useWriteFailure, type WriteFailure } from "@/components/feedback/notice";
import { notify } from "@/components/feedback/toast";
import { browserApi } from "@/lib/api/browser";
import { isApiError } from "@/lib/api/errors";
import { LOGIN_PATH } from "@/lib/config";

import { utcDay } from "./model";
import { orgKeys, type OrgWrite, writeOrg } from "./queries";

function signInAgainOn401(error: unknown) {
  if (isApiError(error) && error.kind === "unauthorized") window.location.assign(LOGIN_PATH);
}

/** One write of the chart from the browser; success and failure alike read the chart again, so the page shows the hub's. */
export function useOrgWrite() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (write: OrgWrite) => writeOrg(browserApi(), write),
    onError: signInAgainOn401,
    onSettled: () => queryClient.invalidateQueries({ queryKey: orgKeys.all }),
  });
}

/**
 * What to say when a write of the chart fails: the page's words for the status, and below them the hub's own reason,
 * which names the rule that refused it (who may change a unit, a level above the viewer's own, a seat already there).
 */
export function useOrgFailure() {
  const failure = useWriteFailure();
  const t = useTranslations("org.errors");
  return useCallback(
    (error: unknown): WriteFailure => {
      const info = isApiError(error) ? error.info : null;
      const failed = failure(error, { 403: t("forbidden"), 404: t("notFound"), 409: t("conflict"), 422: t("invalid") });
      const status = info?.status ?? 0;
      const refused = status === 403 || status === 404 || status === 409 || status === 422;
      return refused && info?.message ? { ...failed, detail: t("hubReason", { message: info.message }) } : failed;
    },
    [failure, t],
  );
}

/**
 * A dialog's write: runs it, keeps the failure to show inside the dialog, and on success says so in a toast and
 * returns true, so the dialog closes.
 */
export function useOrgSubmit() {
  const write = useOrgWrite();
  const failure = useOrgFailure();
  const [error, setError] = useState<WriteFailure | null>(null);
  const run = useCallback(
    async (change: OrgWrite, success: string): Promise<boolean> => {
      setError(null);
      try {
        await write.mutateAsync(change);
      } catch (caught) {
        setError(failure(caught));
        return false;
      }
      notify({ tone: "success", text: success });
      return true;
    },
    [write, failure],
  );
  return { run, pending: write.isPending, error };
}

const noChange = () => () => {};
const today = () => utcDay(Date.now());
const notYet = () => null;

/** Today in UTC by the browser's clock, for seats that ended or end soon; null on the server and while hydrating. */
export function useToday(): string | null {
  return useSyncExternalStore(noChange, today, notYet);
}
