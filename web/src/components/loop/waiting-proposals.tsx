"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, MessageSquare, X } from "lucide-react";
import { useTranslations } from "next-intl";
import { useId, useState } from "react";

import { Identifier } from "@/components/data/identifier";
import { When } from "@/components/data/when";
import { ConfirmAction } from "@/components/feedback/confirm-action";
import { useWriteFailure } from "@/components/feedback/notice";
import { notify, notifyFailure } from "@/components/feedback/toast";
import { Button } from "@/components/ui/button";
import { browserApi } from "@/lib/api/browser";
import { isApiError } from "@/lib/api/errors";
import { LOGIN_PATH } from "@/lib/config";
import { loopKeys } from "@/lib/loop-queries";
import { opportunitiesHref, opportunityKeys } from "@/lib/opportunity-queries";
import { problemKeys, problemsHref } from "@/lib/problem-queries";
import {
  answerProposal,
  type BuilderProposal,
  type ProposalAnswer,
  proposalMakes,
  waitingKeys,
  type WaitingKind,
  waitingProposalsQuery,
} from "@/lib/waiting-proposals";

/**
 * A proposal answered by a writer; then every list of waiting proposals, the problems or opportunities and the goals
 * again, whether it worked or not, so a 409 drops what another writer answered first.
 */
function useAnswer(project: string, kind: WaitingKind) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ id, answer }: { id: number; answer: ProposalAnswer }) => answerProposal(browserApi(), project, id, answer),
    onError: (error) => {
      if (isApiError(error) && error.kind === "unauthorized") window.location.assign(LOGIN_PATH);
    },
    onSettled: () =>
      Promise.all([
        queryClient.invalidateQueries({ queryKey: waitingKeys.all(project) }),
        queryClient.invalidateQueries({ queryKey: kind === "problem" ? problemKeys.all(project) : opportunityKeys.all(project) }),
        queryClient.invalidateQueries({ queryKey: loopKeys.goalList(project) }),
      ]),
  });
}

/** What the proposal holds besides its statement: who the problem hurts, or who gains from the opportunity. */
function subject(sets: Record<string, unknown>, kind: WaitingKind): string | null {
  const value = kind === "problem" ? sets.who : sets.beneficiary;
  return typeof value === "string" && value.trim() ? value : null;
}

function WaitingProposal({ project, kind, proposal }: { project: string; kind: WaitingKind; proposal: BuilderProposal }) {
  const t = useTranslations("waitingProposals");
  const words = useTranslations(kind === "problem" ? "problems.list" : "opportunities.list");
  const failure = useWriteFailure();
  const answer = useAnswer(project, kind);
  const [rejecting, setRejecting] = useState(false);
  const makes = proposalMakes(proposal, kind);
  const code = makes.code ?? "";
  const who = subject(makes.sets, kind);
  const overrides = { 403: t("forbidden"), 409: t("conflict") };
  const approve = async () => {
    try {
      const found = await answer.mutateAsync({ id: proposal.id, answer: "approve" });
      if (found.state === "applied") {
        const text = makes.what === "evidence" ? t("approvedLines", { code }) : t("approved", { code });
        const href = kind === "problem" ? problemsHref(project, code) : opportunitiesHref(project, code);
        notify({ tone: "success", text, link: code ? { label: t("open", { code }), href } : null });
      } else notify({ tone: "error", text: t("failed", { id: proposal.id }), detail: found.error });
    } catch (error) {
      notifyFailure(t("failed", { id: proposal.id }), failure(error, overrides));
    }
  };
  const reject = async () => {
    try {
      await answer.mutateAsync({ id: proposal.id, answer: "reject" });
      setRejecting(false);
      notify({ tone: "success", text: t("rejected", { id: proposal.id }) });
    } catch {
      // The reason shows in the dialog, which stays open.
    }
  };
  const lines = makes.lines;
  return (
    <li className="flex flex-col gap-3 px-4 py-3 md:flex-row md:items-start md:gap-4" data-testid="waiting-proposal" data-proposal={proposal.id}>
      <div className="flex min-w-0 flex-1 flex-col gap-1">
        <div className="flex min-w-0 items-center gap-2">
          {code ? <Identifier value={code} className="shrink-0" /> : null}
          <span className="min-w-0 truncate text-sm font-medium text-foreground" title={makes.statement ?? proposal.summary}>
            {makes.statement ?? proposal.summary}
          </span>
        </div>
        <p className="text-xs leading-4 text-fg-subtle" data-testid="waiting-proposal-meta">
          {t(`makes.${makes.what}`, { code, lines: lines.length })}
          {who ? `. ${kind === "problem" ? words("who", { who }) : words("beneficiary", { who })}` : null}
          {". "}
          {t("proposedBy", { login: proposal.proposed_by })} <When value={proposal.proposed_at} short />
        </p>
        {lines.length > 0 ? (
          <details className="group text-xs" data-testid="waiting-proposal-lines">
            <summary className="w-fit cursor-pointer text-muted-foreground hover:text-foreground">{t("lines", { count: lines.length })}</summary>
            <ul role="list" className="mt-2 flex flex-col gap-2 border-l pl-3">
              {lines.map((line) => (
                <li key={`${line.source_uri}#${line.locator ?? ""}#${line.excerpt}`} className="flex flex-col gap-0.5">
                  <span className="text-foreground">{line.excerpt}</span>
                  <span className="font-mono text-[11px] break-all text-fg-subtle">{t("line", { kind: line.source_kind, signal: line.signal, uri: line.source_uri })}</span>
                </li>
              ))}
            </ul>
          </details>
        ) : null}
      </div>
      <div className="flex shrink-0 flex-wrap items-center gap-2">
        {proposal.link ? (
          <Button asChild variant="ghost" size="sm">
            <a href={proposal.link} data-testid="waiting-proposal-thread">
              <MessageSquare aria-hidden="true" />
              {t("thread")}
            </a>
          </Button>
        ) : null}
        <Button type="button" size="sm" disabled={answer.isPending} onClick={() => void approve()} data-testid="waiting-proposal-approve">
          <Check aria-hidden="true" />
          {answer.isPending && !rejecting ? t("approving") : t("approve")}
        </Button>
        <Button
          type="button"
          size="sm"
          variant="secondary"
          disabled={answer.isPending}
          onClick={() => {
            answer.reset();
            setRejecting(true);
          }}
          data-testid="waiting-proposal-reject"
        >
          <X aria-hidden="true" />
          {t("reject")}
        </Button>
      </div>
      <ConfirmAction
        open={rejecting}
        onOpenChange={setRejecting}
        title={t("rejectTitle", { id: proposal.id })}
        description={<p>{t("rejectText")}</p>}
        confirmLabel={t("reject")}
        pendingLabel={t("rejecting")}
        cancelLabel={t("cancel")}
        pending={answer.isPending}
        error={answer.isError && rejecting ? failure(answer.error, overrides) : null}
        onConfirm={() => void reject()}
        testId="waiting-proposal-reject-dialog"
      />
    </li>
  );
}

/**
 * The proposals of a problem or an opportunity that wait for the visitor's answer, above the list of the Problems or
 * Opportunities page: what each makes, with its lines of evidence, and Approve and Reject. A proposal an intake made has
 * no thread to answer in, so a writer answers it here or with `evo-agents hub intake proposal`. Renders nothing while
 * none waits, and in a project that does not enable the plugin builder (its route answers 404); a page shows it to a
 * writer alone, for whom alone the hub lists any.
 */
export function WaitingProposals({ project, kind }: { project: string; kind: WaitingKind }) {
  const t = useTranslations("waitingProposals");
  const heading = useId();
  const { data } = useQuery(waitingProposalsQuery(browserApi, project, kind));
  if (!data || data.length === 0) return null;
  return (
    <section
      aria-labelledby={heading}
      className="flex min-w-0 flex-col overflow-hidden rounded-md border bg-card shadow-raised"
      data-testid="waiting-proposals"
      data-kind={kind}
    >
      <header className="flex flex-col gap-1 border-b px-4 py-3">
        <h2 id={heading} className="text-sm font-medium text-foreground">
          {t("title", { count: data.length })}
        </h2>
        <p className="text-xs leading-4 text-fg-subtle">{t(kind === "problem" ? "problemText" : "opportunityText")}</p>
      </header>
      <ul role="list" className="flex flex-col divide-y">
        {data.map((proposal) => (
          <WaitingProposal key={proposal.id} project={project} kind={kind} proposal={proposal} />
        ))}
      </ul>
    </section>
  );
}
