"use client";

import { CircleCheck, CircleDashed, CircleSlash, Copy, Gavel, Info, Layers, Lightbulb, type LucideIcon, Split, Waypoints } from "lucide-react";
import { useTranslations } from "next-intl";

import { Tag } from "@/components/data/identifier";
import { type StatusLook, StatusPill, type StatusPillProps } from "@/components/status/status-badge";
import { cn } from "@/lib/utils";

import type { FindingKind, FindingLevel, FindingStatus } from "./findings-model";

/**
 * A finding's tags and pill, beside badges.tsx: only the review page shows findings, so their icons stay out of the
 * chunk every SRS page loads (web/DESIGN.md, the Findings card), with their words under `srs.findings` in both message
 * files.
 */

const LEVEL_ICONS: Record<FindingLevel, LucideIcon> = { decide: Gavel, advisory: Lightbulb, info: Info };

/** A finding's level as a tag, as a task's priority is one; decide in `attention`, since a person settles it. */
export function FindingLevelTag({ level }: { level: FindingLevel }) {
  const t = useTranslations("srs.findings.level");
  const Icon = LEVEL_ICONS[level];
  return (
    <Tag data-level={level} data-testid="srs-finding-level" className={cn(level === "decide" && "bg-attention-soft text-attention")}>
      <Icon aria-hidden="true" />
      {t(level)}
    </Tag>
  );
}

const KIND_ICONS: Record<FindingKind, LucideIcon> = { duplicate: Copy, contradiction: Split, kg_conflict: Waypoints, overlap: Layers };

/** What a finding is: a duplicate, a contradiction, a conflict with the knowledge graph, an overlap. */
export function FindingKindTag({ kind }: { kind: FindingKind }) {
  const t = useTranslations("srs.findings.kind");
  const Icon = KIND_ICONS[kind];
  return (
    <Tag data-kind={kind} data-testid="srs-finding-kind">
      <Icon aria-hidden="true" />
      {t(kind)}
    </Tag>
  );
}

/** Where a finding stands: open until settled, resolved (green), or waived, a last state drawn as an outline. */
export const FINDING_LOOKS = {
  open: { tone: "neutral", icon: CircleDashed },
  resolved: { tone: "success", icon: CircleCheck },
  waived: { tone: "outline", icon: CircleSlash },
} as const satisfies Record<FindingStatus, StatusLook>;

export function FindingStatusBadge({ status, ...props }: StatusPillProps & { status: FindingStatus }) {
  const t = useTranslations("srs.findings.status");
  return <StatusPill look={FINDING_LOOKS[status]} label={t(status)} data-status={status} data-testid="srs-finding-status" {...props} />;
}
