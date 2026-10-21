"use client";

import {
  Archive,
  Ban,
  Bot,
  CircleCheck,
  CircleDashed,
  CircleHelp,
  CircleX,
  ClipboardCheck,
  FileText,
  FlaskConical,
  Gauge,
  type LucideIcon,
  MessageSquareQuote,
  Mic,
  Minus,
  RadioTower,
  Sparkles,
  Store,
  ThumbsDown,
  ThumbsUp,
  TimerOff,
  UserRound,
} from "lucide-react";
import type { Route } from "next";
import { useTranslations } from "next-intl";

import { BrandMark } from "@/components/brand";
import { Identifier, Tag } from "@/components/data/identifier";
import { type StatusLook, StatusPill, type StatusPillProps } from "@/components/status/status-badge";
import { cn } from "@/lib/utils";

import type { BaselineStatus, CanvasProvenance, ClaimStatus, Confidence, Direction, EvidenceKind, Verdict } from "./api";

/**
 * The canvas pages' pills and tags. Only these pages show a claim's status, so its looks live here rather than in
 * `STATUS_LOOKS` (web/DESIGN.md, components/canvas): an assumption is neutral, testing is being checked (violet), a
 * validated claim is green, an invalidated one red, a retired one an outline. Amber is kept for what needs a person: a
 * baseline that waits for review, a test card past its deadline.
 */
export const STATUS_LOOKS = {
  assumption: { tone: "neutral", icon: CircleDashed },
  testing: { tone: "review", icon: FlaskConical },
  validated: { tone: "success", icon: CircleCheck },
  invalidated: { tone: "danger", icon: CircleX },
  retired: { tone: "outline", icon: Archive },
} as const satisfies Record<ClaimStatus, StatusLook>;

export function ClaimStatusBadge({ status, ...props }: StatusPillProps & { status: ClaimStatus }) {
  const t = useTranslations("canvas.status");
  return <StatusPill look={STATUS_LOOKS[status]} label={t(status)} data-status={status} data-testid="claim-status" {...props} />;
}

const BASELINE_LOOKS = {
  proposed: { tone: "attention", icon: ClipboardCheck },
  approved: { tone: "success", icon: CircleCheck },
  expired: { tone: "outline", icon: TimerOff },
} as const satisfies Record<BaselineStatus, StatusLook>;

export function BaselineStatusBadge({ status, ...props }: StatusPillProps & { status: BaselineStatus }) {
  const t = useTranslations("canvas.baselineStatus");
  return <StatusPill look={BASELINE_LOOKS[status]} label={t(status)} data-status={status} data-testid="baseline-status" {...props} />;
}

/** A claim's code (`CL-3`) in a mono chip, linking to its page when given one. */
export function ClaimCode({ code, href, testId = "claim-code" }: { code: string; href?: Route; testId?: string }) {
  return <Identifier value={code} href={href} testId={testId} data-claim={code} />;
}

/** How sure the writer is of a claim, as a tag with a gauge. */
export function ConfidenceTag({ confidence }: { confidence: Confidence }) {
  const t = useTranslations("canvas.confidence");
  return (
    <Tag data-confidence={confidence} title={t("title", { confidence: t(confidence) })}>
      <Gauge aria-hidden="true" />
      {t(confidence)}
    </Tag>
  );
}

const KIND_ICONS: Record<EvidenceKind, LucideIcon> = {
  signal: RadioTower,
  feedback: MessageSquareQuote,
  interview: Mic,
  experiment: FlaskConical,
  market: Store,
  doc: FileText,
  simulation: Bot,
};

export function EvidenceKindTag({ kind }: { kind: EvidenceKind }) {
  const t = useTranslations("canvas.evidenceKind");
  const Icon = KIND_ICONS[kind];
  return (
    <Tag data-kind={kind} data-testid="evidence-kind">
      <Icon aria-hidden="true" />
      {t(kind)}
    </Tag>
  );
}

const DIRECTION_ICONS: Record<Direction, LucideIcon> = { supports: ThumbsUp, contradicts: ThumbsDown, neutral: Minus };

/** Which way a piece of evidence points: in words beside its icon, the tone of its text only a second cue. */
export function DirectionTag({ direction }: { direction: Direction }) {
  const t = useTranslations("canvas.direction");
  const Icon = DIRECTION_ICONS[direction];
  return (
    <Tag
      data-direction={direction}
      data-testid="evidence-direction"
      className={cn(direction === "supports" && "bg-success-soft text-success", direction === "contradicts" && "bg-danger-soft text-danger")}
    >
      <Icon aria-hidden="true" />
      {t(direction)}
    </Tag>
  );
}

const VERDICT_ICONS: Record<Verdict, LucideIcon> = { supports: ThumbsUp, contradicts: ThumbsDown, irrelevant: Ban };

export function VerdictTag({ verdict }: { verdict: Verdict | null }) {
  const t = useTranslations("canvas.verdict");
  const Icon = verdict ? VERDICT_ICONS[verdict] : CircleHelp;
  return (
    <Tag data-verdict={verdict ?? "waiting"} data-testid="judging-verdict" className={cn(verdict === null && "bg-attention-soft text-attention")}>
      <Icon aria-hidden="true" />
      {t(verdict ?? "waiting")}
    </Tag>
  );
}

/** Evidence or a proposal resting on simulated people, as a tag the eye catches before its words. */
export function SyntheticTag() {
  const t = useTranslations("canvas.evidence");
  return (
    <Tag data-testid="synthetic" className="bg-review-soft text-review" title={t("syntheticTitle")}>
      <Sparkles aria-hidden="true" />
      {t("synthetic")}
    </Tag>
  );
}

/** Who wrote a rev or a piece of evidence: a person by login, an agent for the member it worked for, or the hub. */
export function Provenance({ provenance }: { provenance: CanvasProvenance }) {
  const t = useTranslations("canvas.provenance");
  if (provenance.actor === "hub") {
    return (
      <span className="inline-flex min-w-0 items-center gap-1.5" data-actor="hub">
        <BrandMark className="size-4 shrink-0" />
        <span className="font-medium text-foreground">{t("hub")}</span>
      </span>
    );
  }
  const Icon = provenance.actor === "agent" ? Bot : UserRound;
  const who = provenance.login ?? t("someone");
  return (
    <span className="inline-flex min-w-0 items-center gap-1.5" data-actor={provenance.actor}>
      <Icon className="size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
      <span className="min-w-0 [overflow-wrap:anywhere]">
        {provenance.actor === "agent" ? (
          <>
            <span className="font-medium text-foreground">{t("agent")}</span> {t("forMember", { login: who })}
            {provenance.model ? <span className="text-fg-subtle"> {t("model", { model: provenance.model })}</span> : null}
          </>
        ) : (
          <span className="font-medium text-foreground">{who}</span>
        )}
      </span>
    </span>
  );
}
