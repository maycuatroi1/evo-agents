"use client";

import { useQuery } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useCallback } from "react";

import { useWriteFailure, type WriteFailure } from "@/components/feedback/notice";
import { browserApi } from "@/lib/api/browser";
import { isApiError } from "@/lib/api/errors";
import { LOGIN_PATH } from "@/lib/config";
import { whoamiQuery } from "@/lib/queries";

/** The signed-in visitor's login; null until whoami answered. */
export function useMe(): string | null {
  const { data } = useQuery(whoamiQuery(browserApi));
  return data?.login ?? null;
}

/** The projects where the visitor may add agents to a chat: those they write to (the writer or admin role). */
export function useWriterProjects(): string[] | null {
  const { data } = useQuery(whoamiQuery(browserApi));
  if (!data) return null;
  return data.grants.filter((grant) => grant.role === "writer" || grant.role === "admin").map((grant) => grant.project);
}

/**
 * What to tell the visitor when a write of a chat fails. A chat's refusals say exactly what to change (who is not
 * signed in to the hub, whose grant does not clear the chat's label, which agent is not in it), so the hub's own
 * message comes under the sentence for every refusal, not only 422. On a 401 the page goes to sign in.
 */
export function useChatFailure() {
  const t = useTranslations("chats.errors");
  const failure = useWriteFailure();
  return useCallback(
    (error: unknown): WriteFailure => {
      if (isApiError(error) && error.kind === "unauthorized") window.location.assign(LOGIN_PATH);
      const base = failure(error, { 403: t("forbidden"), 404: t("notFound"), 409: t("conflict"), 422: t("invalid") });
      const message = isApiError(error) ? error.info.message : null;
      const refused = [403, 404, 409, 422].includes(base.status);
      return refused && message ? { ...base, detail: t("hubSays", { message }) } : base;
    },
    [failure, t],
  );
}
