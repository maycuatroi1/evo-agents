"use client";

import { queryOptions, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, Eye, EyeOff, Sparkles, X } from "lucide-react";
import { useTranslations } from "next-intl";

import { useWriteFailure } from "@/components/feedback/notice";
import { notify, notifyFailure } from "@/components/feedback/toast";
import { StatePanel } from "@/components/states/states";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { browserApi } from "@/lib/api/browser";
import { call } from "@/lib/api/client";
import { chatAgentsQuery } from "@/lib/chat-keys";
import type { SpecDiff } from "@/lib/system-diff";
import { systemProposalKeys, type SystemProposal } from "@/lib/system-proposals";
import { systemKeys } from "@/lib/systems-queries";
import { answerProposal, type ProposalAnswer } from "@/lib/waiting-proposals";

import type { DesignerSlots } from "./slots";

/** The core plugin builder and its agent type (`evo_agents/core_plugins/builder`), whose profile is @builder. */
export const BUILDER = "builder";

/**
 * The handle of the project's @builder: the profile its agent type `builder` made when an admin enabled it, which is
 * `builder` in the first project only and `builder-<project>` (or the handle the admin named) in every other one, since
 * a handle is unique on the hub. Null while the type is not enabled.
 */
const builderHandleQuery = (project: string) =>
  queryOptions({
    queryKey: ["systems", "builder-handle", project] as const,
    queryFn: async ({ signal }): Promise<string | null> => {
      const types = await call(browserApi().GET("/v1/projects/{project}/agent-types", { params: { path: { project } }, signal }));
      return types.find((one) => one.plugin === BUILDER && one.name === BUILDER && one.enabled)?.handle ?? null;
    },
  });

type Props = {
  project: string;
  slug: string;
  revision: number;
  canChange: boolean;
  /** The newest proposal of @builder waiting for this system, and its diff against the saved revision. */
  proposal: SystemProposal | null;
  diff: SpecDiff | null;
  /** Whether the canvas draws the proposal. */
  showing: boolean;
  onShowingChange: (showing: boolean) => void;
  onClose: () => void;
  slots: DesignerSlots;
};

/**
 * The Builder panel of the designer: the chat with @builder (the chat home's frame), and the newest system it proposes
 * for this system, which the canvas draws over the saved revision. An admin of the project accepts it, which makes the
 * agent profiles and graphs it names and writes the spec as the next revision through @builder's approval, or discards
 * it, which changes nothing.
 */
export function BuilderPanel({ project, slug, revision, canChange, proposal, diff, showing, onShowingChange, onClose, slots }: Props) {
  const t = useTranslations("systems.builder");
  const queryClient = useQueryClient();
  const failure = useWriteFailure();
  const agents = useQuery(chatAgentsQuery(browserApi));
  const handle = useQuery(builderHandleQuery(project)).data ?? null;
  const builder = agents.data?.find((one) => one.handle === handle && one.project === project) ?? null;
  const answer = useMutation({
    mutationFn: ({ id, said }: { id: number; said: ProposalAnswer }) => answerProposal(browserApi(), project, id, said),
    onSettled: () =>
      Promise.all([
        queryClient.invalidateQueries({ queryKey: systemProposalKeys.of(project) }),
        queryClient.invalidateQueries({ queryKey: systemKeys.one(project, slug) }),
        queryClient.invalidateQueries({ queryKey: systemKeys.list(project) }),
      ]),
  });
  const overrides = { 403: t("forbidden"), 409: t("conflict") };

  const accept = async (id: number) => {
    try {
      const found = await answer.mutateAsync({ id, said: "approve" });
      if (found.state === "applied") notify({ tone: "success", text: t("accepted", { revision: found.applied_version ?? revision + 1 }) });
      else notify({ tone: "error", text: t("failed", { id }), detail: found.error });
    } catch (error) {
      notifyFailure(t("failed", { id }), failure(error, overrides));
    }
  };
  const discard = async (id: number) => {
    try {
      await answer.mutateAsync({ id, said: "reject" });
      notify({ tone: "success", text: t("discarded", { id }) });
    } catch (error) {
      notifyFailure(t("failed", { id }), failure(error, overrides));
    }
  };

  return (
    <aside
      className="relative flex min-h-0 flex-col border-t bg-card max-lg:max-h-[70%] lg:w-[26rem] lg:border-t-0 lg:border-l"
      aria-label={t("panel")}
      data-testid="system-builder-panel"
    >
      <div className="flex flex-col gap-1 border-b px-4 py-3 pr-12">
        <h2 className="flex items-center gap-2 text-sm font-semibold">
          <Sparkles className="size-4 text-muted-foreground" aria-hidden="true" />
          {t("title")}
        </h2>
        <Button type="button" variant="ghost" size="icon-sm" className="absolute top-3 right-3" aria-label={t("close")} onClick={onClose}>
          <X aria-hidden="true" />
        </Button>
      </div>
      {proposal && diff ? (
        <section
          className="flex flex-col gap-2 border-b bg-brand-soft/40 px-4 py-3"
          aria-label={t("proposal", { id: proposal.id })}
          data-testid="system-builder-proposal"
          data-proposal={proposal.id}
        >
          <p className="text-sm font-medium text-pretty">{proposal.summary}</p>
          <p className="text-xs text-muted-foreground" data-testid="system-builder-counts">
            {t("counts", {
              added: diff.counts.added,
              removed: diff.counts.removed,
              changed: diff.counts.changed,
              addedEdges: diff.counts.addedEdges,
              removedEdges: diff.counts.removedEdges,
            })}
          </p>
          {proposal.agents.length || proposal.graphs.length ? (
            <ul className="flex flex-col gap-0.5 text-xs text-muted-foreground">
              {proposal.agents.map((agent) => (
                <li key={`a-${agent.handle}`}>{t("newAgent", { handle: agent.handle, name: agent.name || agent.handle })}</li>
              ))}
              {proposal.graphs.map((graph) => (
                <li key={`g-${graph.name}`}>{t("newGraph", { name: graph.name })}</li>
              ))}
            </ul>
          ) : null}
          {proposal.baseVersion !== revision ? <p className="text-xs text-attention">{t("stale", { base: proposal.baseVersion, revision })}</p> : null}
          <div className="flex flex-wrap items-center gap-2 pt-1">
            <Button type="button" variant="outline" size="sm" onClick={() => onShowingChange(!showing)} aria-pressed={showing} data-testid="system-builder-show">
              {showing ? <EyeOff aria-hidden="true" /> : <Eye aria-hidden="true" />}
              {showing ? t("hide") : t("show")}
            </Button>
            {canChange ? (
              <>
                <Button
                  type="button"
                  size="sm"
                  className="ml-auto"
                  busy={answer.isPending && answer.variables?.said === "approve"}
                  disabled={answer.isPending}
                  onClick={() => void accept(proposal.id)}
                  data-testid="system-builder-accept"
                >
                  <Check aria-hidden="true" />
                  {t("accept")}
                </Button>
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  busy={answer.isPending && answer.variables?.said === "reject"}
                  disabled={answer.isPending}
                  onClick={() => void discard(proposal.id)}
                  data-testid="system-builder-discard"
                >
                  <X aria-hidden="true" />
                  {t("discard")}
                </Button>
              </>
            ) : (
              <span className="ml-auto text-xs text-muted-foreground">{t("adminOnly")}</span>
            )}
          </div>
        </section>
      ) : null}
      {agents.isPending ? (
        <Skeleton className="m-4 h-40" />
      ) : builder ? (
        <div className="flex min-h-[320px] flex-1 flex-col" data-testid="system-builder-chat">
          <slots.TestChat agent={builder} />
        </div>
      ) : (
        <StatePanel icon={Sparkles} title={t("missingTitle")} description={t("missing")} className="m-4 shadow-none" testId="system-builder-missing" />
      )}
    </aside>
  );
}
