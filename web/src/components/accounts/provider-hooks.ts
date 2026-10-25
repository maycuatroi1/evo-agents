"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useCallback, useState } from "react";

import { browserApi } from "@/lib/api/browser";
import { isApiError } from "@/lib/api/errors";
import { runnableModelKeys } from "@/lib/runnable-models";

import { type Failure, signInAgainOn401 } from "./hooks";
import { SECRETS_PAGE_KEY } from "./login-queries";
import {
  changeProviderAccount,
  deleteProviderAccount,
  type ProbeWrite,
  probeProvider,
  type ProviderChange,
  type ProviderProbe,
  type ProviderWrite,
  putProviderAccount,
} from "./provider-queries";
import { accountKeys } from "./queries";

/**
 * The writes of a provider account: add (PUT), change (PATCH), delete, and Test connection or Fetch models (a probe).
 * As with a Claude token, none goes through TanStack Query's mutations, which keep their variables, here a key, in the
 * mutation cache: a body lives only for its call, and the hook keeps whether a write runs and how the last one failed.
 * Every write reloads the accounts, the runnable models and the Secrets page's list, success or failure alike; a 401
 * sends the visitor to sign in.
 */
export function useProviderWrite() {
  const queryClient = useQueryClient();
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const run = useCallback(
    async <T,>(write: () => Promise<T>): Promise<{ ok: true; value: T } | { ok: false }> => {
      setPending(true);
      setError(null);
      let outcome: { ok: true; value: T } | { ok: false } = { ok: false };
      try {
        outcome = { ok: true, value: await write() };
      } catch (caught) {
        signInAgainOn401(caught);
        setError(caught);
      }
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: accountKeys.all }),
        queryClient.invalidateQueries({ queryKey: runnableModelKeys.all }),
        queryClient.invalidateQueries({ queryKey: SECRETS_PAGE_KEY }),
      ]);
      setPending(false);
      return outcome;
    },
    [queryClient],
  );
  const put = useCallback((name: string, body: ProviderWrite) => run(() => putProviderAccount(browserApi(), name, body)), [run]);
  const change = useCallback((name: string, body: ProviderChange) => run(() => changeProviderAccount(browserApi(), name, body)), [run]);
  const remove = useCallback((name: string) => run(() => deleteProviderAccount(browserApi(), name)), [run]);
  const reset = useCallback(() => setError(null), []);
  return { put, change, remove, pending, error, reset };
}

export type ProviderWriter = ReturnType<typeof useProviderWrite>;

/** Test connection or Fetch models: what the endpoint answered, or why the hub could not ask it. */
export function useProbe() {
  const [pending, setPending] = useState(false);
  const [result, setResult] = useState<{ probe: ProviderProbe | null; error: unknown } | null>(null);
  const probe = useCallback(async (target: { body: ProbeWrite } | { name: string; model: string | null }) => {
    setPending(true);
    setResult(null);
    let outcome: { probe: ProviderProbe | null; error: unknown };
    try {
      outcome = { probe: await probeProvider(browserApi(), target), error: null };
    } catch (caught) {
      signInAgainOn401(caught);
      outcome = { probe: null, error: caught };
    }
    setResult(outcome);
    setPending(false);
    return outcome;
  }, []);
  const reset = useCallback(() => setResult(null), []);
  return { probe, pending, result, reset };
}

/**
 * What to say when a write of a provider account fails, by status; the hub's message goes below as a detail. It names
 * the name, project, worker or URL it refused, never the key.
 */
export function useProviderFailure() {
  const t = useTranslations("accounts.provider.errors");
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
