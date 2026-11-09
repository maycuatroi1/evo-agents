"use client";

import { Bot, CircleCheck, Eye, MessageCircleQuestion, TriangleAlert, Undo2, Users } from "lucide-react";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { useId } from "react";

import { Ago } from "@/components/data/ago";
import { Tag } from "@/components/data/identifier";
import { type StatusLook, StatusPill } from "@/components/status/status-badge";
import { Button } from "@/components/ui/button";

import { CodeChip } from "./badges";
import { caseHref, reviewHref } from "./links";
import { type BaLine, baLine, type CardStatus, clip, INTENT_CHARS, type QueueCard, SUMMARY_CHARS, waitsForViewer } from "./review-queue-model";
import { caseName } from "./use-case-model";

/** A card's status as a pill: amber while it needs the viewer, cobalt while @ba works on it, green once approved. */
const STATUS_LOOKS: Record<CardStatus["kind"], StatusLook> = {
  waiting: { tone: "attention", icon: Eye },
  deciding: { tone: "attention", icon: MessageCircleQuestion },
  ba_review: { tone: "running", icon: Bot },
  approved: { tone: "success", icon: CircleCheck },
  asked: { tone: "neutral", icon: Undo2 },
  changes: { tone: "neutral", icon: Undo2 },
  others: { tone: "outline", icon: Users },
};

const STATUS_WORDS = {
  waiting: "waiting",
  deciding: "deciding",
  ba_review: "baReview",
  approved: "approved",
  asked: "asked",
  changes: "changes",
  others: "others",
} as const satisfies Record<CardStatus["kind"], string>;

function StatusChip({ status }: { status: CardStatus }) {
  const t = useTranslations("srs.queue.status");
  const label = status.kind === "deciding" ? t("deciding", { count: status.count }) : t(STATUS_WORDS[status.kind]);
  return <StatusPill look={STATUS_LOOKS[status.kind]} label={label} data-status={status.kind} data-testid="srs-queue-status" />;
}

/** One line about @ba's check of the version: still running, what it found, that it stopped, or nothing. */
function BaNote({ line }: { line: BaLine }) {
  const t = useTranslations("srs.queue.ba");
  if (!line) return null;
  const tone = line.kind === "here" || line.kind === "elsewhere" ? "text-attention" : "text-muted-foreground";
  const Icon = line.kind === "broke" ? TriangleAlert : Bot;
  const text = line.kind === "here" || line.kind === "elsewhere" ? t(line.kind, { count: line.count }) : t(line.kind);
  return (
    <p className={`flex items-start gap-1.5 text-[13px] leading-[18px] ${tone}`} data-testid="srs-queue-ba" data-ba={line.kind}>
      <Icon className="mt-px size-3.5 shrink-0" aria-hidden="true" />
      <span className="min-w-0 [overflow-wrap:anywhere]">{text}</span>
    </p>
  );
}

/** A labelled line of a card: "This time:" and what changed, "Goal:" and the use case's intent. */
function Line({ label, text, testId }: { label: string; text: string; testId: string }) {
  return (
    <p className="text-[13px] leading-[18px] text-pretty [overflow-wrap:anywhere]" data-testid={testId}>
      <span className="font-medium text-foreground">{label}</span> <span className="text-muted-foreground">{text}</span>
    </p>
  );
}

/**
 * A card of the review queue: the status in plain words, who sent the version and when, the use case's title over its
 * code and document, what this version changed and the use case's goal, a line about @ba's check, and one button that
 * opens the use case's review ("Read and review", or "View" for someone who may not review the version). A version that
 * changes no use case is one card too, opening the version's review.
 */
export function QueueCardView({ project, card, primary }: { project: string; card: QueueCard; primary: boolean }) {
  const t = useTranslations("srs.queue");
  const titleId = useId();
  const { version, useCase, status } = card;
  const href = useCase ? caseHref(project, version.document, version.number, useCase.key) : reviewHref(project, version.document, version.number);
  const name = useCase ? caseName(useCase) : version.document_title;
  const title = useCase ? useCase.title : t("wholeTitle");
  // The author rule is each use case's: who wrote a revision of this one may still review the others.
  const act = useCase ? useCase.may_review : version.may_review;
  return (
    <article
      aria-labelledby={titleId}
      className="flex min-w-0 flex-col gap-3 rounded-md bg-card p-4 shadow-raised ring-1 ring-border"
      data-testid="srs-queue-case"
      data-document={version.document}
      data-version={version.number}
      data-key={useCase?.key ?? ""}
      data-state={useCase?.state ?? "version"}
      data-status={status.kind}
    >
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5">
        <StatusChip status={status} />
        <p className="text-[13px] leading-[18px] text-muted-foreground" data-testid="srs-queue-byline">
          {t("sent", { author: version.author })}
          {version.submitted_at ? (
            <>
              {" "}
              <Ago value={version.submitted_at} never="" />
            </>
          ) : null}
        </p>
      </div>
      <div className="flex min-w-0 flex-col gap-1">
        <h3 id={titleId} className="text-base leading-6 font-semibold text-balance [overflow-wrap:anywhere]">
          {title}
        </h3>
        <p className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted-foreground">
          {useCase?.code ? <CodeChip code={useCase.rev ? `${useCase.code} r${useCase.rev}` : useCase.code} /> : null}
          <span className="min-w-0 [overflow-wrap:anywhere]">{version.document_title}</span>
          {useCase?.change === "added" ? <Tag data-testid="srs-queue-change">{t("added")}</Tag> : null}
          {useCase?.change === "removed" ? <Tag data-testid="srs-queue-change">{t("removed")}</Tag> : null}
        </p>
      </div>
      <div className="flex min-w-0 flex-col gap-1.5">
        <Line label={t("thisTime")} text={version.summary.trim() ? clip(version.summary, SUMMARY_CHARS) : t("noSummary")} testId="srs-queue-summary" />
        {useCase?.intent ? <Line label={t("goal")} text={clip(useCase.intent, INTENT_CHARS)} testId="srs-queue-intent" /> : null}
        {useCase ? null : <p className="text-[13px] leading-[18px] text-muted-foreground">{t("wholeText")}</p>}
        <BaNote line={baLine(version, useCase)} />
      </div>
      <div className="flex justify-end">
        <Button asChild variant={primary && waitsForViewer(status) ? "default" : "secondary"}>
          <Link href={href} aria-label={act ? t("readFor", { name }) : t("viewFor", { name })} data-testid="srs-queue-review">
            {act ? t("read") : t("view")}
          </Link>
        </Button>
      </div>
    </article>
  );
}
