"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useCallback, useState } from "react";

import { useWriteFailure, type WriteFailure } from "@/components/admin/notice";
import { browserApi } from "@/lib/api/browser";
import { isApiError } from "@/lib/api/errors";
import { LOGIN_PATH } from "@/lib/config";

import { deleteSecret, putSecret, secretKeys, type SecretWrite, type SecretWritten } from "./queries";

function signInAgainOn401(error: unknown) {
  if (isApiError(error) && error.kind === "unauthorized") window.location.assign(LOGIN_PATH);
}

/**
 * A secret's PUT from the browser. It does not go through TanStack Query's mutations, which keep their variables (here
 * the value) in the mutation cache after the write: the body lives only for the call, and the hook keeps no more than
 * whether a write runs and how the last one failed. Success and failure alike reload the list, so the page shows what
 * the hub holds; a 401 sends the visitor to sign in.
 */
export function usePutSecret() {
  const queryClient = useQueryClient();
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<unknown>(null);

  const put = useCallback(
    async (name: string, body: SecretWrite): Promise<SecretWritten | null> => {
      setPending(true);
      setError(null);
      let written: SecretWritten | null = null;
      try {
        written = await putSecret(browserApi(), name, body);
      } catch (caught) {
        signInAgainOn401(caught);
        setError(caught);
      }
      await queryClient.invalidateQueries({ queryKey: secretKeys.all });
      setPending(false);
      return written;
    },
    [queryClient],
  );
  const reset = useCallback(() => setError(null), []);
  return { put, pending, error, reset };
}

export type PutSecret = ReturnType<typeof usePutSecret>;

/** A secret's DELETE from the browser: the name is all it carries. The list is read again either way. */
export function useDeleteSecret() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (name: string) => deleteSecret(browserApi(), name),
    onError: signInAgainOn401,
    onSettled: () => queryClient.invalidateQueries({ queryKey: secretKeys.all }),
  });
}

type Overrides = Partial<Record<403 | 404 | 409 | 422, string>>;

/**
 * What to say when a write of a secret fails: the shared wording, with the secrets' own texts for 403 to 503. The hub's
 * message goes below as a detail: it names the project or worker it refused, never a value.
 */
export function useSecretFailure() {
  const failure = useWriteFailure();
  const t = useTranslations("secrets.errors");
  return useCallback(
    (error: unknown, overrides: Overrides = {}): WriteFailure => {
      const info = isApiError(error) ? error.info : null;
      const status = info?.status ?? 0;
      const detail = info?.message ? t("apiMessage", { message: info.message }) : null;
      if (status === 503) return { text: t("unavailable"), detail, requestId: info?.requestId ?? null, status };
      const failed = failure(error, { 403: t("forbidden"), 404: t("notFound"), 409: t("conflict"), ...overrides });
      return status === 403 || status === 404 || status === 409 ? { ...failed, detail } : failed;
    },
    [failure, t],
  );
}
