"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useCallback } from "react";

import { useWriteFailure, type WriteFailure } from "@/components/feedback/notice";
import { agentKeys, createAgent, deleteAgent, deleteAvatar, editAgent, putAvatar } from "@/lib/agent-queries";
import type { Agent, AgentCreate, AgentEdit } from "@/lib/agents";
import { browserApi } from "@/lib/api/browser";
import { isApiError } from "@/lib/api/errors";
import { LOGIN_PATH } from "@/lib/config";
import { whoamiQuery } from "@/lib/queries";

/**
 * The writes of the Agents pages. Each reloads the project's agents (the list, the agent's page and its versions)
 * whatever the hub answered, so the pages show what it holds; a 401 sends the visitor to sign in.
 */
function signInAgainOn401(error: unknown) {
  if (isApiError(error) && error.kind === "unauthorized") window.location.assign(LOGIN_PATH);
}

function useAgentWrite<T>(project: string, write: (variables: T) => Promise<Agent>) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: write,
    onError: signInAgainOn401,
    onSettled: () => queryClient.invalidateQueries({ queryKey: agentKeys.all(project) }),
  });
}

export function useCreateAgent(project: string) {
  return useAgentWrite(project, (body: AgentCreate) => createAgent(browserApi(), project, body));
}

export function useEditAgent(project: string, handle: string) {
  return useAgentWrite(project, (body: AgentEdit) => editAgent(browserApi(), project, handle, body));
}

export function useDeleteAgent(project: string, handle: string) {
  return useAgentWrite<void>(project, () => deleteAgent(browserApi(), project, handle));
}

/** Set the avatar (`data`, base64) or, with null, drop it. */
export function useAvatarWrite(project: string) {
  return useAgentWrite(project, ({ handle, data }: { handle: string; data: string | null }) =>
    data === null ? deleteAvatar(browserApi(), project, handle) : putAvatar(browserApi(), project, handle, data),
  );
}

/** Whether the visitor is an admin of `project`: only an admin creates, changes or deletes its agents. */
export function useAgentAdmin(project: string): boolean {
  const { data } = useQuery(whoamiQuery(browserApi));
  return data?.grants.some((grant) => grant.project === project && grant.role === "admin") ?? false;
}

type Overrides = Partial<Record<403 | 404 | 409 | 422, string>>;

/**
 * What to say when a write of an agent fails: the shared wording with the agents' own texts, and the hub's message as
 * the detail of a 403, 404, 409, 413 or 503 too, since it names what it refused (a handle taken, a version moved on).
 */
export function useAgentFailure() {
  const failure = useWriteFailure();
  const t = useTranslations("agents.errors");
  return useCallback(
    (error: unknown, overrides: Overrides = {}): WriteFailure => {
      const info = isApiError(error) ? error.info : null;
      const status = info?.status ?? 0;
      const detail = info?.message ? t("apiMessage", { message: info.message }) : null;
      if (status === 503) return { text: t("unavailable"), detail, requestId: info?.requestId ?? null, status };
      if (status === 413) return { text: t("tooLarge"), detail, requestId: info?.requestId ?? null, status };
      const failed = failure(error, { 403: t("forbidden"), 404: t("notFound"), 409: t("conflict"), 422: t("invalid"), ...overrides });
      return [403, 404, 409].includes(status) ? { ...failed, detail } : failed;
    },
    [failure, t],
  );
}
