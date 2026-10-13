"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { TriangleAlert } from "lucide-react";
import { useTranslations } from "next-intl";
import { useCallback } from "react";

import { browserApi } from "@/lib/api/browser";
import { isApiError } from "@/lib/api/errors";
import { LOGIN_PATH } from "@/lib/config";

import { type AccountChange, type AccountPolicyWrite, accountKeys, changeAccount, deletePolicy, putPolicy } from "./queries";

/** A write that failed, in the words the page shows: what happened, the hub's message and the request id. */
export type Failure = { text: string; detail: string | null; requestId: string | null; status: number };

function signInAgainOn401(error: unknown) {
  if (isApiError(error) && error.kind === "unauthorized") window.location.assign(LOGIN_PATH);
}

/** Every write reloads the accounts and policies, whatever the hub answered, so the page shows what it holds. */
function useSettled() {
  const queryClient = useQueryClient();
  return () => queryClient.invalidateQueries({ queryKey: accountKeys.all });
}

export function useChangeAccount() {
  const onSettled = useSettled();
  return useMutation({
    mutationFn: ({ name, body }: { name: string; body: AccountChange }) => changeAccount(browserApi(), name, body),
    onError: signInAgainOn401,
    onSettled,
  });
}

export function usePutPolicy() {
  const onSettled = useSettled();
  return useMutation({
    mutationFn: ({ project, body }: { project: string; body: AccountPolicyWrite }) => putPolicy(browserApi(), project, body),
    onError: signInAgainOn401,
    onSettled,
  });
}

export function useDeletePolicy() {
  const onSettled = useSettled();
  return useMutation({
    mutationFn: (project: string) => deletePolicy(browserApi(), project),
    onError: signInAgainOn401,
    onSettled,
  });
}

/** What to say when a write fails, by status; the hub's own message goes below as a detail. It never carries a value. */
export function useFailure() {
  const t = useTranslations("accounts.errors");
  return useCallback(
    (error: unknown): Failure => {
      const info = isApiError(error) ? error.info : null;
      const status = info?.status ?? 0;
      const detail = info?.message ? t("apiMessage", { message: info.message }) : null;
      const requestId = info?.requestId ?? null;
      const text =
        status === 0
          ? t("network")
          : status === 403
            ? t("forbidden")
            : status === 404
              ? t("notFound")
              : status === 422
                ? t("invalid")
                : t("server");
      return { text, detail: status === 0 ? null : detail, requestId, status };
    },
    [t],
  );
}

/** A failed write inside a dialog, which stays open. */
export function FailureBox({ failure, requestLabel }: { failure: Failure; requestLabel: string }) {
  return (
    <div
      role="alert"
      data-testid="account-dialog-error"
      className="flex items-start gap-2.5 rounded-md border border-danger/30 bg-danger-soft px-3 py-2.5 text-sm text-danger"
    >
      <TriangleAlert className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
      <div className="flex min-w-0 flex-col gap-1">
        <p className="font-medium text-pretty">{failure.text}</p>
        {failure.detail ? <p className="text-xs break-words">{failure.detail}</p> : null}
        {failure.requestId ? <p className="font-mono text-xs break-all">{requestLabel}</p> : null}
      </div>
    </div>
  );
}
