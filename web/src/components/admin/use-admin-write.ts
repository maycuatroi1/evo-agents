"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";

import { workerKeys } from "@/components/workers/queries";
import { browserApi } from "@/lib/api/browser";
import type { ApiClient } from "@/lib/api/client";
import { isApiError } from "@/lib/api/errors";
import { LOGIN_PATH } from "@/lib/config";
import { queryKeys } from "@/lib/queries";

import { adminKeys } from "./data";

/**
 * An admin write from the browser. Nothing changes on screen until the API answers: success and failure alike
 * reload every admin list, the project list, who the visitor is (a grant can change their own access) and the
 * workers (revoking a worker's token revokes the worker), so the page always shows what the hub holds, including
 * after a 404 or 409 caused by someone else's change. A 401 means the session ended: the visitor goes to sign in.
 */
export function useAdminWrite<TArgs, TResult>(write: (api: ApiClient, args: TArgs) => Promise<TResult>) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (args: TArgs) => write(browserApi(), args),
    onError: (error) => {
      if (isApiError(error) && error.kind === "unauthorized") window.location.assign(LOGIN_PATH);
    },
    onSettled: () =>
      Promise.all([
        queryClient.invalidateQueries({ queryKey: adminKeys.all }),
        queryClient.invalidateQueries({ queryKey: queryKeys.projects }),
        queryClient.invalidateQueries({ queryKey: queryKeys.whoami }),
        queryClient.invalidateQueries({ queryKey: workerKeys.all }),
      ]),
  });
}
