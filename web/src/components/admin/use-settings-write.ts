"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";

import { browserApi } from "@/lib/api/browser";
import type { ApiClient } from "@/lib/api/client";
import { isApiError } from "@/lib/api/errors";
import { LOGIN_PATH } from "@/lib/config";
import { settingsKeys } from "@/lib/hub-settings";

/**
 * A write of a setting from the browser. Success and failure alike reload the admin's view of the settings and what
 * the shell reads (the branding, the features), so the sidebar drops or brings back an entry at once. A change of
 * the branding also renders the server's part of the page again (`refresh`): its colours, title and favicon come
 * from the root layout. A 401 means the session ended: the visitor goes to sign in.
 */
export function useSettingsWrite<TArgs, TResult>(
  write: (api: ApiClient, args: TArgs) => Promise<TResult>,
  { refresh = false }: { refresh?: boolean } = {},
) {
  const queryClient = useQueryClient();
  const router = useRouter();
  return useMutation({
    mutationFn: (args: TArgs) => write(browserApi(), args),
    onError: (error) => {
      if (isApiError(error) && error.kind === "unauthorized") window.location.assign(LOGIN_PATH);
    },
    onSettled: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["admin", "settings"] }),
        queryClient.invalidateQueries({ queryKey: settingsKeys.branding }),
        queryClient.invalidateQueries({ queryKey: settingsKeys.features }),
      ]);
      if (refresh) router.refresh();
    },
  });
}
