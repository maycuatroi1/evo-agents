"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { useCallback } from "react";

import { useWriteFailure, type WriteFailure } from "@/components/admin/notice";
import { inboxKeys } from "@/components/inbox/queries";
import { browserApi } from "@/lib/api/browser";
import { isApiError } from "@/lib/api/errors";
import { LOGIN_PATH } from "@/lib/config";
import { queryKeys, whoamiQuery } from "@/lib/queries";

import { type CuratorRights, curatorRights } from "./model";
import {
  answerProposal,
  type CharterWrite,
  type CuratorStatus,
  curatorKeys,
  pauseCurator,
  type Proposal,
  type ProposalAnswer,
  proposalQuery,
  resumeCurator,
  writeCharter,
} from "./queries";

/** The signed-in member and their role on `project` (whoami, which the app's layout reads). */
export function useCuratorViewer(project: string): { login: string; role: string | null } | null {
  const { data } = useQuery(whoamiQuery(browserApi));
  if (!data) return null;
  return { login: data.login, role: data.grants.find((grant) => grant.project === project)?.role ?? null };
}

/** What the visitor may do on the Curator's pages of `project`, from whoami and the night shift's schedules. */
export function useCuratorRights(project: string, status: Pick<CuratorStatus, "schedules"> | null): CuratorRights {
  return curatorRights(status, useCuratorViewer(project));
}

function unauthorized(error: unknown): boolean {
  if (isApiError(error) && error.kind === "unauthorized") {
    window.location.assign(LOGIN_PATH);
    return true;
  }
  return false;
}

/**
 * Pause or resume the night shift. The hub answers with where it stands, which replaces what the page shows; then
 * everything the Curator's pages and Home read of the project is read again (a pause cancels the runs it queued).
 */
export function usePauseCurator(project: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (pause: boolean) => (pause ? pauseCurator(browserApi(), project) : resumeCurator(browserApi(), project)),
    onError: unauthorized,
    onSuccess: (status) => queryClient.setQueryData(curatorKeys.status(project), status),
    onSettled: () =>
      Promise.all([
        queryClient.invalidateQueries({ queryKey: curatorKeys.all(project) }),
        queryClient.invalidateQueries({ queryKey: queryKeys.overview }),
        queryClient.invalidateQueries({ queryKey: ["projects", project, "runs"] }),
      ]),
  });
}

/** Write a new revision of the charter; the night shift's status and the revisions are read again. */
export function useWriteCharter(project: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: CharterWrite) => writeCharter(browserApi(), project, body),
    onError: unauthorized,
    onSuccess: (charter) => queryClient.setQueryData(curatorKeys.charter(project, null), charter),
    onSettled: () =>
      Promise.all([
        queryClient.invalidateQueries({ queryKey: curatorKeys.all(project) }),
        queryClient.invalidateQueries({ queryKey: queryKeys.overview }),
      ]),
  });
}

/**
 * Accept, reject or defer a proposal. The proposal shown becomes the one the hub returned; after a 409 (answered
 * meanwhile) it is read again at once and handed to `onConflict`. Either way the proposals, the night shift's count
 * of what waits, the member's notifications and Home are read again.
 */
export function useAnswerProposal(
  project: string,
  id: number,
  { onConflict }: { onConflict?: (fresh: Proposal) => void } = {},
) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: ProposalAnswer) => answerProposal(browserApi(), project, id, body),
    onError: async (error) => {
      if (unauthorized(error)) return;
      if (isApiError(error) && error.status === 409) {
        const fresh = await queryClient.fetchQuery({ ...proposalQuery(browserApi, project, id), staleTime: 0 }).catch(() => null);
        if (fresh) onConflict?.(fresh);
      }
    },
    onSuccess: (answered) => queryClient.setQueryData(curatorKeys.proposal(project, id), answered),
    onSettled: () =>
      Promise.all([
        queryClient.invalidateQueries({ queryKey: curatorKeys.proposals(project) }),
        queryClient.invalidateQueries({ queryKey: curatorKeys.status(project) }),
        queryClient.invalidateQueries({ queryKey: curatorKeys.all(project), predicate: (query) => query.queryKey[3] === "nights" }),
        queryClient.invalidateQueries({ queryKey: inboxKeys.all }),
        queryClient.invalidateQueries({ queryKey: queryKeys.overview }),
      ]),
  });
}

/** What to say when a write of the Curator fails: 403, 404 and 409 in the Curator's own words, the hub's message kept. */
export function useCuratorFailure() {
  const failure = useWriteFailure();
  const t = useTranslations("curator.errors");
  return useCallback(
    (error: unknown, what: "pause" | "charter" | "answer"): WriteFailure => {
      const base = failure(error, { 403: t(`forbidden.${what}`), 404: t("notFound"), 409: t(`conflict.${what}`) });
      if (base.status === 403 || base.status === 409 || base.status === 413) {
        const message = isApiError(error) ? error.info.message : "";
        return { ...base, detail: message ? t("apiMessage", { message }) : null };
      }
      return base;
    },
    [failure, t],
  );
}
