"use client";

import { Check, Lightbulb, X } from "lucide-react";
import { useTranslations } from "next-intl";
import { type FormEvent, useId, useState } from "react";

import { When } from "@/components/data/when";
import { InlineError } from "@/components/feedback/notice";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { browserApi } from "@/lib/api/browser";

import { acceptProposal, type CanvasProposal, rejectProposal } from "./api";
import { ClaimCode, Provenance, SyntheticTag } from "./badges";
import { SectionCard } from "./chrome";
import { FieldChanges } from "./field-diff";
import { useCanvasWrite } from "./hooks";
import { claimHref } from "./links";
import { LIMITS } from "./model";

/** What a new claim would hold, as the changes of a claim that had nothing. */
function createdFields(proposal: CanvasProposal) {
  return Object.entries(proposal.fields)
    .filter(([, value]) => value !== null && value !== undefined && !(Array.isArray(value) && value.length === 0))
    .map(([field, after]) => ({ field, before: null, after }));
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

function ProposalRow({ project, slug, proposal, writer }: { project: string; slug: string; proposal: CanvasProposal; writer: boolean }) {
  const t = useTranslations("canvas.proposals");
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
  const changes = proposal.kind === "create" ? createdFields(proposal) : proposal.changes;
  return (
    <li className="flex min-w-0 flex-col gap-2.5 border-t pt-3 first:border-t-0 first:pt-0" data-testid="proposal" data-proposal={proposal.id} data-kind={proposal.kind}>
      <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 text-[13px]">
        {proposal.claim ? (
          <>
            <ClaimCode code={proposal.claim} href={claimHref(project, slug, proposal.claim)} />
            <span className="text-muted-foreground">{t("change", { rev: proposal.base_rev ?? 0 })}</span>
          </>
        ) : (
          <span className="font-medium text-foreground">{t("newClaim")}</span>
        )}
        {proposal.synthetic ? <SyntheticTag /> : null}
        <span className="text-xs text-fg-subtle">
          <When value={proposal.created_at} />
        </span>
      </div>
      <div className="text-xs text-muted-foreground">
        <Provenance provenance={proposal.provenance} />
      </div>
      {proposal.reason ? <p className="text-[13px] text-pretty text-muted-foreground [overflow-wrap:anywhere]">{proposal.reason}</p> : null}
      <FieldChanges changes={changes} testId="proposal-changes" />
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

/**
 * The proposals that wait for a writer: new claims @strategist brainstormed, changes it proposed of its own accord,
 * and changes of status the value check proposes from the signals. Accepting writes a rev as the writer's own; a
 * proposal whose claim changed since its base rev goes stale and leaves this list.
 */
export function ProposalsCard({ project, slug, proposals, writer }: { project: string; slug: string; proposals: readonly CanvasProposal[]; writer: boolean }) {
  const t = useTranslations("canvas.proposals");
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
      <ul className="flex min-w-0 flex-col gap-3">
        {proposals.map((proposal) => (
          <ProposalRow key={proposal.id} project={project} slug={slug} proposal={proposal} writer={writer} />
        ))}
      </ul>
    </SectionCard>
  );
}
