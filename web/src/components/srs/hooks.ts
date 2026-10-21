"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback } from "react";

import { browserApi } from "@/lib/api/browser";
import { isApiError } from "@/lib/api/errors";
import { LOGIN_PATH } from "@/lib/config";
import { whoamiQuery } from "@/lib/queries";

import { srsKeys } from "./api";
import type { Viewer } from "./model";

/** The visitor as the SRS pages need them: their login, to tell their own versions, and their role on the project. */
export function useSrsViewer(project: string): Viewer | null {
  const { data } = useQuery(whoamiQuery(browserApi));
  if (!data) return null;
  const role = data.grants.find((grant) => grant.project === project)?.role ?? null;
  return { login: data.login, role: role === "reader" || role === "writer" || role === "admin" ? role : null };
}

/**
 * Reload what a write to a document may have changed: the document's versions, comparisons and comments, and the list
 * of documents (its latest and approved versions). A 401 sends the visitor to sign in.
 */
export function useDocumentRefresh(project: string, slug: string | null) {
  const queryClient = useQueryClient();
  return useCallback(async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: srsKeys.documents(project), exact: true }),
      slug ? queryClient.invalidateQueries({ queryKey: srsKeys.document(project, slug) }) : Promise.resolve(),
    ]);
  }, [queryClient, project, slug]);
}

export function signInAgainOn401(error: unknown): void {
  if (isApiError(error) && error.kind === "unauthorized") window.location.assign(LOGIN_PATH);
}
