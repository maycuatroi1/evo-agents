"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useState } from "react";

import { useWriteFailure, type WriteFailure } from "@/components/feedback/notice";
import { notify } from "@/components/feedback/toast";
import { browserApi } from "@/lib/api/browser";
import { isApiError } from "@/lib/api/errors";
import { LOGIN_PATH } from "@/lib/config";
import { whoamiQuery } from "@/lib/queries";

import { canvasKeys } from "./api";
import type { Viewer } from "./model";

/** The visitor as the canvas pages need them: their login, to tell what they proposed, and their role on the project. */
export function useCanvasViewer(project: string): Viewer | null {
  const { data } = useQuery(whoamiQuery(browserApi));
  if (!data) return null;
  const role = data.grants.find((grant) => grant.project === project)?.role ?? null;
  return { login: data.login, role: role === "reader" || role === "writer" || role === "admin" ? role : null };
}

export function signInAgainOn401(error: unknown): void {
  if (isApiError(error) && error.kind === "unauthorized") window.location.assign(LOGIN_PATH);
}

/**
 * Reload what a write may have changed: the list of canvases (its counts) and everything of one canvas, its claims,
 * proposals and baselines.
 */
export function useCanvasRefresh(project: string) {
  const queryClient = useQueryClient();
  return useCallback(
    async (slug: string | null) => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: canvasKeys.canvases(project), exact: true }),
        slug ? queryClient.invalidateQueries({ queryKey: canvasKeys.canvas(project, slug) }) : Promise.resolve(),
      ]);
    },
    [project, queryClient],
  );
}

/**
 * A write that a form or a dialog runs: busy while it runs, the refusal kept for the form to say beside itself (and
 * the error, for the 409 that names claims), a toast with what `write` returns once it worked, then `done`.
 */
export function useCanvasWrite(project: string, slug: string | null) {
  const failure = useWriteFailure();
  const refresh = useCanvasRefresh(project);
  const [busy, setBusy] = useState(false);
  const [refused, setRefused] = useState<{ failure: WriteFailure; error: unknown } | null>(null);
  const run = useCallback(
    async (write: () => Promise<string | null>, done?: () => void): Promise<boolean> => {
      setBusy(true);
      setRefused(null);
      try {
        const said = await write();
        if (said) notify({ tone: "success", text: said });
        done?.();
        return true;
      } catch (error) {
        signInAgainOn401(error);
        setRefused({ failure: failure(error), error });
        return false;
      } finally {
        setBusy(false);
        await refresh(slug);
      }
    },
    [failure, refresh, slug],
  );
  const clear = useCallback(() => setRefused(null), []);
  return { busy, refused, run, clear };
}
