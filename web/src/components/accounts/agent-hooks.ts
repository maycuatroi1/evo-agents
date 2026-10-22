"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useCallback } from "react";

import { browserApi } from "@/lib/api/browser";
import { whoamiQuery } from "@/lib/queries";

import {
  type AgentAccountChange,
  type AgentAccountWrite,
  changeAgentAccount,
  deleteAgentAccount,
  putAgentAccount,
} from "./agent-queries";
import { type Failure, signInAgainOn401, useFailure } from "./hooks";
import { accountKeys } from "./queries";

/**
 * The writes of an agent's accounts. Each reloads every list of accounts (the agent's card, the Accounts page and its
 * group of agents' accounts) whatever the hub answered, so the pages show what it holds; a 401 sends the visitor to
 * sign in.
 */
function useAgentAccountWrite<T>(write: (variables: T) => Promise<unknown>) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: write,
    onError: signInAgainOn401,
    onSettled: () => queryClient.invalidateQueries({ queryKey: accountKeys.all }),
  });
}

export function usePutAgentAccount(project: string, handle: string) {
  return useAgentAccountWrite(({ name, body }: { name: string; body: AgentAccountWrite }) => putAgentAccount(browserApi(), { project, handle, name }, body));
}

export function useChangeAgentAccount(project: string, handle: string) {
  return useAgentAccountWrite(({ name, body }: { name: string; body: AgentAccountChange }) =>
    changeAgentAccount(browserApi(), { project, handle, name }, body),
  );
}

export function useDeleteAgentAccount(project: string, handle: string) {
  return useAgentAccountWrite((name: string) => deleteAgentAccount(browserApi(), { project, handle, name }));
}

/** Whether the visitor is an admin of `project`: only an admin changes the accounts of its agents. */
export function useProjectAdmin(project: string): boolean {
  const { data } = useQuery(whoamiQuery(browserApi));
  return data?.grants.some((grant) => grant.project === project && grant.role === "admin") ?? false;
}

/** What to say when a write of an agent's account fails: the Accounts page's words, with who may change one. */
export function useAgentAccountFailure() {
  const failureOf = useFailure();
  const t = useTranslations("accounts.agent.errors");
  return useCallback((error: unknown): Failure => failureOf(error, { 403: t("forbidden"), 409: t("conflict") }), [failureOf, t]);
}
