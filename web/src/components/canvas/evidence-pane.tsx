"use client";

import { BadgeCheck, CircleSlash, ExternalLink, Plus, ShieldCheck, Undo2 } from "lucide-react";
import type { Route } from "next";
import Link from "next/link";
import { useFormatter, useTranslations } from "next-intl";
import { type FormEvent, useId, useState } from "react";

import { When } from "@/components/data/when";
import { InlineError } from "@/components/feedback/notice";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { browserApi } from "@/lib/api/browser";
import { signalsHref } from "@/lib/signal-queries";
import { cn } from "@/lib/utils";

import { type CanvasClaimDetail, type CanvasEvidence, confirmEvidence, withdrawEvidence } from "./api";
import { DirectionTag, EvidenceKindTag, Provenance, SyntheticTag } from "./badges";
import { SectionCard } from "./chrome";
import { EvidenceForm } from "./evidence-form";
import { useCanvasWrite } from "./hooks";
import { AGGREGATES, LIMITS } from "./model";

const LINK = "inline-flex items-center gap-1 text-brand underline underline-offset-4 transition-colors hover:text-brand-hover";

/**
 * A piece's link: for what the hub wrote, its own Signals page (`checks.link`, from the hub's public URL or a path),
 * opened here; otherwise an http(s) page out of the hub, in a new tab. Anything else is not a link.
 */
function EvidenceLink({ url, hub }: { url: string; hub: boolean }) {
  const t = useTranslations("canvas.evidence");
  if (hub && /^(https?:\/\/[^/]+)?\/p\/[^/]+\/signals(\?|$)/i.test(url)) {
    const path = url.slice(url.indexOf("/p/"));
    return (
      <Link href={path as Route} className={LINK} data-testid="evidence-link">
        {t("openSignals")}
      </Link>
    );
  }
  if (!/^https?:\/\//i.test(url)) return null;
  return (
    <a href={url} target="_blank" rel="noopener noreferrer" className={cn(LINK, "max-w-full min-w-0")} data-testid="evidence-link">
      <span className="truncate">{url}</span>
      <ExternalLink className="size-3.5 shrink-0" aria-hidden="true" />
      <span className="sr-only">{t("newTab")}</span>
    </a>
  );
}

/** What a piece the hub wrote rests on: the check of the test card, its value against the threshold, and the result. */
function Basis({ project, basis }: { project: string; basis: Record<string, unknown> }) {
  const t = useTranslations("canvas.basis");
  const tCard = useTranslations("canvas.card");
  const format = useFormatter();
  const number = (value: unknown) => (typeof value === "number" ? format.number(value, { maximumFractionDigits: 4 }) : "?");
  const metric = typeof basis.metric === "string" ? basis.metric : null;
  const tenant = typeof basis.tenant === "string" ? basis.tenant : null;
  const result = typeof basis.result === "string" && ["met", "not_met", "insufficient"].includes(basis.result) ? (basis.result as "met" | "not_met" | "insufficient") : null;
  const aggregate = AGGREGATES.find((name) => name === basis.aggregate) ?? "last";
  return (
    <div className="flex min-w-0 flex-col gap-1 rounded-sm bg-surface-sunken px-3 py-2 text-xs text-muted-foreground" data-testid="evidence-basis" data-result={result ?? undefined}>
      <p className="[overflow-wrap:anywhere]">
        {basis.check === "feedback"
          ? t("feedback", { value: number(basis.value), samples: number(basis.samples), days: number(basis.window_days) })
          : t("number", { aggregate: tCard(`aggregates.${aggregate}`), value: number(basis.value), samples: number(basis.samples), days: number(basis.window_days) })}{" "}
        {t("against", { comparator: String(basis.comparator ?? ""), threshold: number(basis.threshold) })}
        {result ? (
          <>
            {": "}
            <span className={cn("font-medium", result === "met" ? "text-success" : result === "not_met" ? "text-danger" : "text-foreground")}>{t(result)}</span>
          </>
        ) : null}
      </p>
      {metric && metric !== "feedback" ? (
        <Link href={signalsHref(project, metric, tenant)} className={cn(LINK, "w-fit font-mono")} data-testid="evidence-basis-signals">
          {tenant ? t("signalsOfTenant", { metric, tenant }) : metric}
        </Link>
      ) : null}
    </div>
  );
}

function WithdrawForm({ onWithdraw, busy, onCancel }: { onWithdraw: (reason: string) => void; busy: boolean; onCancel: () => void }) {
  const t = useTranslations("canvas.evidence");
  const id = useId();
  const [reason, setReason] = useState("");
  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (reason.trim()) onWithdraw(reason.trim());
  };
  return (
    <form onSubmit={submit} className="flex flex-col gap-2 sm:flex-row sm:items-end" data-testid="evidence-withdraw-form">
      <div className="flex min-w-0 flex-1 flex-col gap-1.5">
        <Label htmlFor={id}>{t("withdrawReason")}</Label>
        <Input id={id} value={reason} onChange={(event) => setReason(event.target.value)} maxLength={LIMITS.reason} aria-required="true" autoFocus />
      </div>
      <div className="flex gap-2">
        <Button type="button" variant="ghost" onClick={onCancel}>
          {t("cancel")}
        </Button>
        <Button type="submit" variant="secondary" busy={busy} disabled={!reason.trim()} data-testid="evidence-withdraw-confirm">
          {t("withdraw")}
        </Button>
      </div>
    </form>
  );
}

/** Whether a piece is real evidence, in words: a shield in green, or why it is not. */
function Realness({ evidence }: { evidence: CanvasEvidence }) {
  const t = useTranslations("canvas.evidence");
  if (evidence.real) {
    return (
      <span className="inline-flex items-center gap-1 text-xs font-medium text-success" data-testid="evidence-real">
        <ShieldCheck className="size-3.5" aria-hidden="true" />
        {t("real")}
      </span>
    );
  }
  return (
    <span className="inline-flex min-w-0 items-center gap-1 text-xs text-muted-foreground" data-testid="evidence-not-real">
      <CircleSlash className="size-3.5 shrink-0" aria-hidden="true" />
      <span className="[overflow-wrap:anywhere]">{t("notReal", { why: evidence.not_real_because ?? "" })}</span>
    </span>
  );
}

function EvidenceItem({ project, slug, claim, evidence, writer }: { project: string; slug: string; claim: string; evidence: CanvasEvidence; writer: boolean }) {
  const t = useTranslations("canvas.evidence");
  const format = useFormatter();
  const { busy, refused, run } = useCanvasWrite(project, slug);
  const [withdrawing, setWithdrawing] = useState(false);
  const withdrawn = evidence.withdrawn_at !== null;
  const confirmable = writer && !withdrawn && evidence.provenance.actor === "agent" && evidence.confirmed_by === null;
  const confirm = () => void run(async () => (await confirmEvidence(browserApi(), project, slug, claim, evidence.id), t("confirmed", { id: evidence.id })));
  const withdraw = (reason: string) =>
    void run(async () => (await withdrawEvidence(browserApi(), project, slug, claim, evidence.id, reason), t("withdrawn", { id: evidence.id })), () => setWithdrawing(false));
  return (
    <li className={cn("flex min-w-0 flex-col gap-2 border-t pt-3 first:border-t-0 first:pt-0", withdrawn && "opacity-70")} data-testid="evidence" data-evidence={evidence.id} data-kind={evidence.kind}>
      <div className="flex min-w-0 flex-wrap items-center gap-1.5">
        <EvidenceKindTag kind={evidence.kind} />
        <DirectionTag direction={evidence.direction} />
        {evidence.synthetic ? <SyntheticTag /> : null}
        <span className="ml-auto">
          <Realness evidence={evidence} />
        </span>
      </div>
      <p className={cn("text-[13px] text-pretty whitespace-pre-line text-foreground [overflow-wrap:anywhere]", withdrawn && "line-through decoration-fg-subtle")}>{evidence.summary}</p>
      {evidence.basis ? <Basis project={project} basis={evidence.basis} /> : null}
      <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
        <Provenance provenance={evidence.provenance} />
        {evidence.observed_on ? <span>{t("observed", { day: format.dateTime(new Date(`${evidence.observed_on}T00:00:00Z`), { dateStyle: "medium", timeZone: "UTC" }) })}</span> : null}
        <When value={evidence.created_at} />
        {evidence.url ? <EvidenceLink url={evidence.url} hub={evidence.provenance.actor === "hub"} /> : null}
      </div>
      {evidence.confirmed_by ? (
        <p className="inline-flex items-center gap-1 text-xs text-muted-foreground">
          <BadgeCheck className="size-3.5" aria-hidden="true" />
          {t("confirmedBy", { login: evidence.confirmed_by })}
        </p>
      ) : null}
      {withdrawn ? (
        <p className="text-xs text-muted-foreground [overflow-wrap:anywhere]" data-testid="evidence-withdrawn">
          {t("withdrawnBy", { login: evidence.withdrawn_by ?? "", reason: evidence.withdrawn_reason ?? "" })}
        </p>
      ) : null}
      {refused ? <InlineError text={refused.failure.text} detail={refused.failure.detail} requestId={refused.failure.requestId} /> : null}
      {writer && !withdrawn ? (
        withdrawing ? (
          <WithdrawForm onWithdraw={withdraw} busy={busy} onCancel={() => setWithdrawing(false)} />
        ) : (
          <div className="flex flex-wrap gap-2">
            {confirmable ? (
              <Button type="button" size="sm" variant="outline" onClick={confirm} busy={busy} data-testid="evidence-confirm">
                <BadgeCheck aria-hidden="true" />
                {t("confirm")}
              </Button>
            ) : null}
            <Button type="button" size="sm" variant="ghost" onClick={() => setWithdrawing(true)} disabled={busy} data-testid="evidence-withdraw">
              <Undo2 aria-hidden="true" />
              {t("withdraw")}
            </Button>
          </div>
        )
      ) : null}
    </li>
  );
}

/**
 * A claim's evidence, the newest first: each piece's kind, which way it points, whether it is synthetic and whether
 * it is real (and why not), what it says, who added it and when, its link, and what a piece the hub wrote rests on. A
 * writer adds a piece, confirms what an agent added, and withdraws a piece with why; pieces above what the visitor may
 * read are counted only.
 */
export function EvidencePane({ project, slug, claim, writer }: { project: string; slug: string; claim: CanvasClaimDetail; writer: boolean }) {
  const t = useTranslations("canvas.evidence");
  const [adding, setAdding] = useState(false);
  const shown = [...claim.evidence].sort((a, b) => b.id - a.id);
  return (
    <SectionCard
      title={t("heading", { count: claim.evidence.length, real: claim.real_support_count })}
      headingId="claim-evidence-title"
      testId="claim-evidence"
      action={
        writer && !adding ? (
          <Button type="button" size="sm" variant="outline" onClick={() => setAdding(true)} data-testid="evidence-add">
            <Plus aria-hidden="true" />
            {t("add")}
          </Button>
        ) : null
      }
    >
      {adding ? <EvidenceForm project={project} slug={slug} claim={claim.code} onDone={() => setAdding(false)} /> : null}
      {shown.length === 0 ? <p className="text-[13px] text-fg-subtle">{t("none")}</p> : null}
      {shown.length > 0 ? (
        <ul className="flex min-w-0 flex-col gap-3">
          {shown.map((evidence) => (
            <EvidenceItem key={evidence.id} project={project} slug={slug} claim={claim.code} evidence={evidence} writer={writer} />
          ))}
        </ul>
      ) : null}
      {claim.hidden_evidence > 0 ? <p className="text-xs text-fg-subtle">{t("hidden", { count: claim.hidden_evidence })}</p> : null}
    </SectionCard>
  );
}
