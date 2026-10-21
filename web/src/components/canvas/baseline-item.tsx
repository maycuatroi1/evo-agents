"use client";

import { Archive, ArrowRight, CalendarPlus, Check, Clock, X } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";
import { type FormEvent, useId, useState } from "react";

import { When } from "@/components/data/when";
import { InlineError } from "@/components/feedback/notice";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { browserApi } from "@/lib/api/browser";
import { cn } from "@/lib/utils";

import { type CanvasBaselineItem, decideItem, type ItemDecision } from "./api";
import { ClaimCode, ClaimStatusBadge, DirectionTag, EvidenceKindTag, SyntheticTag } from "./badges";
import { useCanvasWrite } from "./hooks";
import { claimHref } from "./links";
import { LIMITS, utcToday } from "./model";

const DECISION_ICONS = { accepted: Check, rejected: X, deferred: Clock, extended: CalendarPlus, retired: Archive } as const;

/** The day after `day` (YYYY-MM-DD), the earliest new deadline an extension takes. */
function dayAfter(day: string): string {
  const next = new Date(`${day}T00:00:00Z`);
  next.setUTCDate(next.getUTCDate() + 1);
  return next.toISOString().slice(0, 10);
}

/**
 * What a reviewer decides of one item: accept, reject or defer a change of status; extend (with a new deadline after
 * today) or retire a test card past its deadline, or defer it. The reason is optional and kept with the rev it writes.
 */
function DecideForm({ project, slug, number, item }: { project: string; slug: string; number: number; item: CanvasBaselineItem }) {
  const t = useTranslations("canvas.decide");
  const ids = useId();
  const { busy, refused, run } = useCanvasWrite(project, slug);
  const [reason, setReason] = useState("");
  const [deadline, setDeadline] = useState("");
  const [tried, setTried] = useState(false);
  const earliest = dayAfter(utcToday());
  const overdue = item.kind === "overdue";
  const badDeadline = tried && overdue && (deadline === "" || deadline < earliest);
  const decide = (decision: ItemDecision) => {
    if (decision === "extended") {
      setTried(true);
      if (deadline === "" || deadline < earliest) return;
    }
    void run(async () => {
      await decideItem(browserApi(), project, slug, number, item.claim, {
        decision,
        deadline: decision === "extended" ? deadline : null,
        reason: reason.trim(),
      });
      return t(`done.${decision}`, { claim: item.claim });
    });
  };
  const choices = item.decisions.filter((decision): decision is ItemDecision => decision in DECISION_ICONS);
  const submit = (event: FormEvent) => event.preventDefault();
  return (
    <form onSubmit={submit} className="flex min-w-0 flex-col gap-3 rounded-md border bg-surface-sunken/50 p-3" data-testid="baseline-decide" aria-label={t("label", { claim: item.claim })}>
      <div className="grid gap-3 sm:grid-cols-[minmax(0,1fr)_auto]">
        <div className="flex min-w-0 flex-col gap-1.5">
          <Label htmlFor={`${ids}-reason`}>{t("reason")}</Label>
          <Input id={`${ids}-reason`} value={reason} onChange={(event) => setReason(event.target.value)} maxLength={LIMITS.reason} placeholder={t("reasonPlaceholder")} data-testid="baseline-reason" />
        </div>
        {overdue && choices.includes("extended") ? (
          <div className="flex flex-col gap-1.5">
            <Label htmlFor={`${ids}-deadline`}>{t("newDeadline")}</Label>
            <Input
              id={`${ids}-deadline`}
              type="date"
              min={earliest}
              value={deadline}
              onChange={(event) => setDeadline(event.target.value)}
              aria-invalid={badDeadline || undefined}
              aria-describedby={badDeadline ? `${ids}-deadline-error` : undefined}
              data-testid="baseline-deadline"
            />
          </div>
        ) : null}
      </div>
      {badDeadline ? (
        <p id={`${ids}-deadline-error`} className="text-xs font-medium text-danger">
          {t("deadlineBad")}
        </p>
      ) : null}
      {refused ? <InlineError text={refused.failure.text} detail={refused.failure.detail} requestId={refused.failure.requestId} /> : null}
      <div className="flex flex-wrap gap-2">
        {choices.map((decision) => {
          const Icon = DECISION_ICONS[decision];
          const main = decision === "accepted" || decision === "extended";
          return (
            <Button
              key={decision}
              type="button"
              size="sm"
              variant={main ? "secondary" : decision === "deferred" ? "ghost" : "outline"}
              onClick={() => decide(decision)}
              disabled={busy}
              data-testid={`baseline-${decision}`}
            >
              <Icon aria-hidden="true" />
              {t(`choice.${decision}`)}
            </Button>
          );
        })}
      </div>
    </form>
  );
}

/** What the item asks, in words: from the status in effect to the one asked, or the deadline a test card passed. */
function Ask({ item }: { item: CanvasBaselineItem }) {
  const t = useTranslations("canvas.item");
  const format = useFormatter();
  if (item.kind === "overdue") {
    const day = item.deadline ? format.dateTime(new Date(`${item.deadline}T00:00:00Z`), { dateStyle: "medium", timeZone: "UTC" }) : "";
    return <p className="text-[13px] text-attention">{t("overdue", { day })}</p>;
  }
  return (
    <div className="flex flex-wrap items-center gap-1.5 text-[13px] text-muted-foreground">
      <span className="sr-only">{t("from")}</span>
      <ClaimStatusBadge status={item.from_status} />
      <ArrowRight className="size-3.5 text-fg-subtle" aria-hidden="true" />
      <span className="sr-only">{t("to")}</span>
      {item.to_status ? <ClaimStatusBadge status={item.to_status} /> : null}
    </div>
  );
}

/** The decision made on an item: what, by whom, when, why, and the new deadline an extension gave. */
function Decided({ item }: { item: CanvasBaselineItem }) {
  const t = useTranslations("canvas.item");
  const tDecision = useTranslations("canvas.decision");
  const format = useFormatter();
  if (!item.decision) return null;
  return (
    <p className="flex flex-wrap items-center gap-x-1.5 gap-y-0.5 text-xs text-muted-foreground" data-testid="baseline-decided" data-decision={item.decision}>
      <span className="font-medium text-foreground">{tDecision(item.decision)}</span>
      {item.decided_by ? <span>{t("by", { login: item.decided_by })}</span> : null}
      {item.decided_at ? <When value={item.decided_at} /> : null}
      {item.new_deadline ? <span>{t("newDeadline", { day: format.dateTime(new Date(`${item.new_deadline}T00:00:00Z`), { dateStyle: "medium", timeZone: "UTC" }) })}</span> : null}
      {item.reason ? <span className="[overflow-wrap:anywhere]">{t("because", { reason: item.reason })}</span> : null}
    </p>
  );
}

/**
 * One item of a baseline: the claim and what the item asks of it, the claim's evidence as it is now (each piece's kind,
 * direction and whether it is real), who may not decide it, then the decision made or the reviewer's form.
 */
export function BaselineItem({ project, slug, number, item, open, viewer }: { project: string; slug: string; number: number; item: CanvasBaselineItem; open: boolean; viewer: string | null }) {
  const t = useTranslations("canvas.item");
  const tKind = useTranslations("canvas.itemKind");
  const waiting = item.decision === null;
  const mine = viewer !== null && item.proposers.includes(viewer);
  return (
    <li className={cn("flex min-w-0 flex-col gap-3 rounded-md border bg-card p-4 shadow-raised", waiting && open && "border-attention/40")} data-testid="baseline-item" data-claim={item.claim} data-kind={item.kind}>
      <div className="flex min-w-0 flex-wrap items-center gap-2">
        <ClaimCode code={item.claim} href={claimHref(project, slug, item.claim)} />
        <span className="text-[13px] font-medium text-foreground">{tKind(item.kind)}</span>
        <span className="ml-auto text-xs text-fg-subtle tabular-nums">{t("atRev", { rev: item.claim_rev })}</span>
      </div>
      <Ask item={item} />
      {item.evidence.length > 0 ? (
        <ul className="flex min-w-0 flex-col gap-1.5" data-testid="baseline-evidence">
          {item.evidence.map((evidence) => (
            <li key={evidence.id} className="flex min-w-0 flex-wrap items-center gap-1.5 text-xs text-muted-foreground">
              <EvidenceKindTag kind={evidence.kind} />
              <DirectionTag direction={evidence.direction} />
              {evidence.synthetic ? <SyntheticTag /> : null}
              <span className={cn("min-w-0 [overflow-wrap:anywhere]", evidence.withdrawn_at && "line-through")}>{evidence.summary}</span>
              <span className={cn("ml-auto shrink-0", evidence.real ? "font-medium text-success" : "text-fg-subtle")}>{evidence.real ? t("real") : t("notReal")}</span>
            </li>
          ))}
        </ul>
      ) : (
        <p className="text-xs text-fg-subtle">{t("noEvidence")}</p>
      )}
      {item.hidden_evidence > 0 ? <p className="text-xs text-fg-subtle">{t("hidden", { count: item.hidden_evidence })}</p> : null}
      {item.proposers.length > 0 ? <p className="text-xs text-muted-foreground">{t("proposers", { logins: item.proposers.join(", ") })}</p> : null}
      <Decided item={item} />
      {waiting && open && item.decisions.length > 0 ? <DecideForm project={project} slug={slug} number={number} item={item} /> : null}
      {waiting && open && item.decisions.length === 0 ? (
        <p className="text-xs text-muted-foreground" data-testid="baseline-not-yours">
          {mine ? t("yours") : t("notYours")}
        </p>
      ) : null}
    </li>
  );
}
