"use client";

import { Check, Lightbulb, X } from "lucide-react";
import { useTranslations } from "next-intl";
import { type FormEvent, useEffect, useId, useState } from "react";

import { Identifier, RunRef, Tag } from "@/components/data/identifier";
import { When } from "@/components/data/when";
import { InlineError } from "@/components/feedback/notice";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { browserApi } from "@/lib/api/browser";
import { signalsHref } from "@/lib/signal-queries";
import { cn } from "@/lib/utils";

import { acceptProposal, type CanvasProposal, type CanvasProvenance, rejectProposal } from "./api";
import { ClaimCode, ConfidenceTag, Provenance, SyntheticTag } from "./badges";
import { Fact, SectionCard } from "./chrome";
import { FieldChanges, useFieldName, useValueWords } from "./field-diff";
import { useCanvasWrite } from "./hooks";
import { claimHref, runHref } from "./links";
import { LIMITS } from "./model";
import { type ProposalGroup, type ProposedClaim, proposalGroups, proposedClaim } from "./proposal-model";
import { TestCardList } from "./test-card-list";

const ANCHOR = /^#proposal-(\d+)$/;

/**
 * The proposal the address names (`#proposal-<id>`, as the Decisions page links one), once the list holds it: scrolled
 * into view and focused, so the writer lands on it; its id, to mark it, else null.
 */
function useAnchoredProposal(proposals: readonly CanvasProposal[]): number | null {
  const [anchored, setAnchored] = useState<number | null>(null);
  useEffect(() => {
    const land = () => {
      const id = Number(ANCHOR.exec(window.location.hash)?.[1] ?? Number.NaN);
      if (!proposals.some((proposal) => proposal.id === id)) return;
      setAnchored(id);
      const row = document.getElementById(`proposal-${id}`);
      const still = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      row?.scrollIntoView({ block: "center", behavior: still ? "auto" : "smooth" });
      row?.focus({ preventScroll: true });
    };
    land();
    window.addEventListener("hashchange", land);
    return () => window.removeEventListener("hashchange", land);
  }, [proposals]);
  return anchored;
}

/** Who made a proposal, or a group of them, and the agent's run it came from. */
function ProposalSource({ project, provenance }: { project: string; provenance: CanvasProvenance }) {
  const t = useTranslations("canvas.proposals");
  return (
    <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted-foreground">
      <Provenance provenance={provenance} />
      {provenance.run_id ? (
        <span className="inline-flex items-center gap-1">
          {t("inRun")}
          <RunRef id={provenance.run_id} href={runHref(project, provenance.run_id)} label={t("openRun", { id: provenance.run_id })} className="text-xs" data-testid="proposal-source-run" />
        </span>
      ) : null}
    </div>
  );
}

/**
 * A proposed new claim as the canvas would hold it: its text first, its test card as the claim's page shows one, then
 * the signal keys, use cases and problems it links, and any other field it sets.
 */
function NewClaimPreview({ project, claim }: { project: string; claim: ProposedClaim }) {
  const tCard = useTranslations("canvas.card");
  const tLinks = useTranslations("canvas.links");
  const name = useFieldName();
  const words = useValueWords();
  const cardId = useId();
  const linked = claim.signalKeys.length > 0 || claim.useCases.length > 0 || claim.problems.length > 0 || claim.other.length > 0;
  return (
    <div className="flex min-w-0 flex-col gap-3" data-testid="proposal-claim">
      {claim.text ? (
        <p className="max-w-prose text-sm leading-[22px] font-medium text-pretty text-foreground [overflow-wrap:anywhere]" data-field="text">
          {claim.text}
        </p>
      ) : null}
      {claim.testCard ? (
        <section aria-labelledby={cardId} className="flex min-w-0 flex-col gap-2 rounded-md bg-surface-sunken px-3 py-2.5" data-field="test_card">
          <h3 id={cardId} className="text-xs font-semibold text-foreground">
            {tCard("heading")}
          </h3>
          <TestCardList project={project} card={claim.testCard} className="gap-y-2.5" />
        </section>
      ) : null}
      {linked ? (
        <dl className="grid min-w-0 gap-x-6 gap-y-2.5 sm:grid-cols-2">
          {claim.signalKeys.length > 0 ? (
            <Fact term={tLinks("signalKeys")} testId="proposal-signal-keys">
              <span className="flex flex-wrap gap-1">
                {claim.signalKeys.map((key) => (
                  <Identifier key={key} value={key} href={signalsHref(project, key)} data-key={key} />
                ))}
              </span>
            </Fact>
          ) : null}
          {claim.useCases.length > 0 ? (
            <Fact term={tLinks("useCases")}>
              <span className="flex flex-wrap gap-1">
                {claim.useCases.map((code) => (
                  <Identifier key={code} value={code} data-code={code} />
                ))}
              </span>
            </Fact>
          ) : null}
          {claim.problems.length > 0 ? (
            <Fact term={tLinks("problems")}>
              <span className="flex flex-wrap gap-1">
                {claim.problems.map((code) => (
                  <Identifier key={code} value={code} data-code={code} />
                ))}
              </span>
            </Fact>
          ) : null}
          {claim.other.map(({ field, value }) => (
            <Fact key={field} term={name(field)}>
              <span data-field={field}>{words(field, value)}</span>
            </Fact>
          ))}
        </dl>
      ) : null}
    </div>
  );
}

function RejectForm({ onReject, busy, onCancel }: { onReject: (reason: string) => void; busy: boolean; onCancel: () => void }) {
  const t = useTranslations("canvas.proposals");
  const id = useId();
  const [reason, setReason] = useState("");
  const submit = (event: FormEvent) => {
    event.preventDefault();
    onReject(reason.trim());
  };
  return (
    <form onSubmit={submit} className="flex flex-col gap-2 sm:flex-row sm:items-end" data-testid="proposal-reject-form">
      <div className="flex min-w-0 flex-1 flex-col gap-1.5">
        <Label htmlFor={id}>{t("rejectReason")}</Label>
        <Input id={id} value={reason} onChange={(event) => setReason(event.target.value)} maxLength={LIMITS.reason} autoFocus />
      </div>
      <div className="flex gap-2">
        <Button type="button" variant="ghost" onClick={onCancel}>
          {t("cancel")}
        </Button>
        <Button type="submit" variant="secondary" busy={busy} data-testid="proposal-reject-confirm">
          {t("reject")}
        </Button>
      </div>
    </form>
  );
}

function ProposalRow({
  project,
  slug,
  proposal,
  writer,
  anchored,
  standalone,
}: {
  project: string;
  slug: string;
  proposal: CanvasProposal;
  writer: boolean;
  anchored: boolean;
  /** Whether the row says who made it and why itself, rather than its group above it. */
  standalone: boolean;
}) {
  const t = useTranslations("canvas.proposals");
  const tCell = useTranslations("canvas.cell");
  const { busy, refused, run } = useCanvasWrite(project, slug);
  const [rejecting, setRejecting] = useState(false);
  const accept = () =>
    void run(async () => {
      const done = await acceptProposal(browserApi(), project, slug, proposal.id);
      return t("accepted", { claim: done.claim ?? "" });
    });
  const reject = (reason: string) =>
    void run(
      async () => {
        await rejectProposal(browserApi(), project, slug, proposal.id, reason);
        return t("rejected", { id: proposal.id });
      },
      () => setRejecting(false),
    );
  const created = proposal.kind === "create" ? proposedClaim(proposal.fields) : null;
  return (
    <li
      id={`proposal-${proposal.id}`}
      tabIndex={-1}
      className={cn(
        "flex min-w-0 scroll-mt-20 flex-col gap-2.5 border-t pt-4 outline-none first:border-t-0 first:pt-0",
        anchored && "-mx-2 rounded-md bg-attention-soft/50 px-2 pb-3 ring-2 ring-attention/40 first:pt-3",
      )}
      data-testid="proposal"
      data-proposal={proposal.id}
      data-kind={proposal.kind}
      data-anchored={anchored ? "true" : undefined}
    >
      <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 text-[13px]">
        {proposal.claim ? (
          <>
            <ClaimCode code={proposal.claim} href={claimHref(project, slug, proposal.claim)} />
            <span className="text-muted-foreground">{t("change", { rev: proposal.base_rev ?? 0 })}</span>
          </>
        ) : (
          <span className="font-medium text-foreground">{t("newClaim")}</span>
        )}
        {created?.cell ? (
          <Tag data-cell={created.cell} data-testid="proposal-cell">
            {tCell(created.cell)}
          </Tag>
        ) : null}
        {created?.confidence ? <ConfidenceTag confidence={created.confidence} /> : null}
        {proposal.synthetic ? <SyntheticTag /> : null}
        <span className="ml-auto text-xs text-fg-subtle">
          <When value={proposal.created_at} />
        </span>
      </div>
      {standalone ? <ProposalSource project={project} provenance={proposal.provenance} /> : null}
      {standalone && proposal.reason ? <p className="max-w-prose text-[13px] text-pretty text-muted-foreground [overflow-wrap:anywhere]">{proposal.reason}</p> : null}
      {created ? <NewClaimPreview project={project} claim={created} /> : <FieldChanges changes={proposal.changes} testId="proposal-changes" />}
      {refused ? <InlineError text={refused.failure.text} detail={refused.failure.detail} requestId={refused.failure.requestId} /> : null}
      {writer ? (
        rejecting ? (
          <RejectForm onReject={reject} busy={busy} onCancel={() => setRejecting(false)} />
        ) : (
          <div className="flex flex-wrap gap-2">
            <Button type="button" size="sm" onClick={accept} busy={busy} data-testid="proposal-accept">
              <Check aria-hidden="true" />
              {t("accept")}
            </Button>
            <Button type="button" size="sm" variant="outline" onClick={() => setRejecting(true)} disabled={busy} data-testid="proposal-reject">
              <X aria-hidden="true" />
              {t("reject")}
            </Button>
          </div>
        )
      ) : null}
    </li>
  );
}

/** Proposals one writer made for one reason: who and why once, over the proposals themselves. */
function ProposalGroupItem({
  project,
  slug,
  group,
  writer,
  anchored,
}: {
  project: string;
  slug: string;
  group: ProposalGroup<CanvasProposal>;
  writer: boolean;
  anchored: number | null;
}) {
  const t = useTranslations("canvas.proposals");
  const [first] = group.proposals;
  if (!first) return null;
  if (group.proposals.length === 1) {
    return <ProposalRow project={project} slug={slug} proposal={first} writer={writer} anchored={anchored === first.id} standalone />;
  }
  return (
    <li className="flex min-w-0 flex-col gap-4 border-t pt-4 first:border-t-0 first:pt-0" data-testid="canvas-proposal-group">
      <div className="flex min-w-0 flex-col gap-1.5 border-l-2 border-border-strong pl-3">
        <div className="flex min-w-0 flex-wrap items-center justify-between gap-x-3 gap-y-1">
          <ProposalSource project={project} provenance={first.provenance} />
          <span className="text-xs font-medium text-muted-foreground tabular-nums">
            {t("group", { count: group.proposals.length })}
          </span>
        </div>
        <p className="max-w-prose text-[13px] text-pretty text-muted-foreground [overflow-wrap:anywhere]" data-testid="canvas-proposal-group-reason">
          {group.reason}
        </p>
      </div>
      <ul className="flex min-w-0 flex-col gap-4">
        {group.proposals.map((proposal) => (
          <ProposalRow key={proposal.id} project={project} slug={slug} proposal={proposal} writer={writer} anchored={anchored === proposal.id} standalone={false} />
        ))}
      </ul>
    </li>
  );
}

/**
 * The proposals that wait for a writer: new claims @strategist brainstormed, changes it proposed of its own accord,
 * and changes of status the value check proposes from the signals. Accepting writes a rev as the writer's own; a
 * proposal whose claim changed since its base rev goes stale and leaves this list. Proposals one writer made for one
 * reason (a brainstorm's) sit under that reason, said once; a new claim reads as the claim it would make, a change as
 * its fields before and after. The one the address names (`#proposal-<id>`, the Decisions page's link) is scrolled to
 * and marked.
 */
export function ProposalsCard({ project, slug, proposals, writer }: { project: string; slug: string; proposals: readonly CanvasProposal[]; writer: boolean }) {
  const t = useTranslations("canvas.proposals");
  const anchored = useAnchoredProposal(proposals);
  if (proposals.length === 0) return null;
  return (
    <SectionCard
      title={
        <span className="inline-flex items-center gap-2">
          <Lightbulb className="size-4 text-attention" aria-hidden="true" />
          {t("heading", { count: proposals.length })}
        </span>
      }
      headingId="canvas-proposals-title"
      testId="canvas-proposals"
    >
      <ul className="flex min-w-0 flex-col gap-4">
        {proposalGroups(proposals).map((group) => (
          <ProposalGroupItem key={group.key} project={project} slug={slug} group={group} writer={writer} anchored={anchored} />
        ))}
      </ul>
    </SectionCard>
  );
}
