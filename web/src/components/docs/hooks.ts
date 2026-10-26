"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback } from "react";

import { browserApi } from "@/lib/api/browser";
import { isApiError } from "@/lib/api/errors";
import { LOGIN_PATH } from "@/lib/config";
import { whoamiQuery } from "@/lib/queries";

import { docsKeys } from "./api";
import type { Viewer } from "./model";

/** The visitor as the Docs pages need them: their login and their role on the project. */
export function useDocsViewer(project: string): Viewer | null {
  const { data } = useQuery(whoamiQuery(browserApi));
  if (!data) return null;
  const role = data.grants.find((grant) => grant.project === project)?.role ?? null;
  return { login: data.login, role: role === "reader" || role === "writer" || role === "admin" ? role : null };
}

/** Reload what a save may have changed: the document's versions and the list of documents. */
export function useDocumentRefresh(project: string) {
  const queryClient = useQueryClient();
  return useCallback(
    async (slug: string) => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: docsKeys.documents(project), exact: true }),
        queryClient.invalidateQueries({ queryKey: docsKeys.document(project, slug) }),
      ]);
    },
    [queryClient, project],
  );
}

export function signInAgainOn401(error: unknown): void {
  if (isApiError(error) && error.kind === "unauthorized") window.location.assign(LOGIN_PATH);
}
