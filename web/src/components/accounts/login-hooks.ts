"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useCallback, useState } from "react";

import { browserApi } from "@/lib/api/browser";
import { isApiError } from "@/lib/api/errors";

import { type Failure, signInAgainOn401 } from "./hooks";
import { putAccountToken, SECRETS_PAGE_KEY, type SecretWrite, type SecretWritten } from "./login-queries";
import { accountKeys } from "./queries";

/**
 * An account's token, written with PUT /v1/secrets/{name}. It does not go through TanStack Query's mutations, which
 * keep their variables (here the token) in the mutation cache after the write: the body lives only for the call, and
 * the hook keeps no more than whether a write runs and how the last one failed. Success and failure alike reload the
 * accounts and the Secrets page's list; a 401 sends the visitor to sign in.
 */
export function useWriteToken() {
  const queryClient = useQueryClient();
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const put = useCallback(
    async (name: string, body: SecretWrite): Promise<SecretWritten | null> => {
      setPending(true);
      setError(null);
      let written: SecretWritten | null = null;
      try {
        written = await putAccountToken(browserApi(), name, body);
      } catch (caught) {
        signInAgainOn401(caught);
        setError(caught);
      }
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: accountKeys.all }),
        queryClient.invalidateQueries({ queryKey: SECRETS_PAGE_KEY }),
      ]);
      setPending(false);
      return written;
    },
    [queryClient],
  );
  const reset = useCallback(() => setError(null), []);
  return { put, pending, error, reset };
}

export type WriteToken = ReturnType<typeof useWriteToken>;

/**
 * What to say when a token's write fails, by status, in the words of an account; the hub's message goes below as a
 * detail. It names the project or worker it refused, never the token.
 */
export function useTokenFailure() {
  const t = useTranslations("accounts.login.errors");
  const tShared = useTranslations("accounts.errors");
  return useCallback(
    (error: unknown): Failure => {
      const info = isApiError(error) ? error.info : null;
      const status = info?.status ?? 0;
      const words: Record<number, string> = {
        403: t("forbidden"),
        404: t("notFound"),
        409: t("conflict"),
        422: t("invalid"),
        503: t("unavailable"),
      };
      const text = status === 0 ? tShared("network") : (words[status] ?? tShared("server"));
      const detail = status !== 0 && info?.message ? tShared("apiMessage", { message: info.message }) : null;
      return { text, detail, requestId: info?.requestId ?? null, status };
    },
    [t, tShared],
  );
}
