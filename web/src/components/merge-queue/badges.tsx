"use client";

import {
  Ban,
  CircleCheck,
  CircleDashed,
  CirclePause,
  CircleSlash,
  CircleX,
  Clock,
  Eye,
  GitBranch,
  GitMerge,
  Hammer,
  Hourglass,
  ListChecks,
  Lock,
  type LucideIcon,
  Power,
  PowerOff,
  Puzzle,
  Rewind,
  Rocket,
  Scale,
  SearchCheck,
  Tag as TagIcon,
  Timer,
  Undo2,
} from "lucide-react";
import { useTranslations } from "next-intl";

import { Tag } from "@/components/data/identifier";
import { OtherStatusBadge, type StatusLook, StatusPill, type StatusPillProps } from "@/components/status/status-badge";
import { type CheckResult, isMergeState, type MergeSource, MERGE_SOURCES, type MergeState, type SampleResult } from "@/lib/merge-queue";

/**
 * The states of the merge queue's pages, as pills: only these pages and the Inbox's merge notices show them, so their
 * looks live here rather than in `STATUS_LOOKS`, each tone as web/DESIGN.md spends it. Running means the hub or a
 * Judge works on the merge request now; review, merged and followed to its checks; attention, a person is needed
 * (blocked, paused by the breaker, a breaker tripped, a sample to answer); danger, a check failed and the hub undoes it.
 */
export const MERGE_STATE_LOOKS = {
  gating: { tone: "neutral", icon: Hourglass },
  judging: { tone: "running", icon: Scale },
  ready: { tone: "neutral", icon: Clock },
  rebasing: { tone: "running", icon: GitBranch },
  testing: { tone: "running", icon: Timer },
  merging: { tone: "running", icon: GitMerge },
  merged: { tone: "review", icon: GitMerge },
  verifying: { tone: "review", icon: Eye },
  releasing: { tone: "review", icon: Rocket },
  done: { tone: "success", icon: CircleCheck },
  rolling_back: { tone: "danger", icon: Rewind },
  reverting: { tone: "danger", icon: Undo2 },
  reverted: { tone: "outline", icon: Undo2 },
  blocked: { tone: "attention", icon: Lock },
  paused: { tone: "attention", icon: CirclePause },
  closed: { tone: "outline", icon: CircleSlash },
} as const satisfies Record<MergeState, StatusLook>;

export function MergeStateBadge({ state, ...props }: StatusPillProps & { state: string }) {
  const t = useTranslations("mergeQueue.state");
  if (!isMergeState(state)) return <OtherStatusBadge raw={state} data-testid="merge-state" {...props} />;
  return <StatusPill look={MERGE_STATE_LOOKS[state]} label={t(state)} data-status={state} data-testid="merge-state" {...props} />;
}

const BREAKER_LOOKS = {
  armed: { tone: "success", icon: Power },
  tripped: { tone: "attention", icon: PowerOff },
} as const satisfies Record<"armed" | "tripped", StatusLook>;

/** A repo's breaker: armed lets the queue merge; tripped stops it until an admin resets it. */
export function BreakerBadge({ state, ...props }: StatusPillProps & { state: string }) {
  const t = useTranslations("mergeQueue.breaker.state");
  if (state !== "armed" && state !== "tripped") return <OtherStatusBadge raw={state} data-testid="breaker-state" {...props} />;
  return <StatusPill look={BREAKER_LOOKS[state]} label={t(state)} data-status={state} data-testid="breaker-state" {...props} />;
}

const SAMPLE_LOOKS = {
  waiting: { tone: "attention", icon: SearchCheck },
  ok: { tone: "success", icon: CircleCheck },
  faulty: { tone: "danger", icon: CircleX },
} as const satisfies Record<"waiting" | SampleResult, StatusLook>;

/** An audit sample: waiting for an admin's answer, or answered ok or faulty. */
export function SampleBadge({ result, ...props }: StatusPillProps & { result: string | null }) {
  const t = useTranslations("mergeQueue.sample.state");
  const key = result === "ok" || result === "faulty" ? result : "waiting";
  return <StatusPill look={SAMPLE_LOOKS[key]} label={t(key)} data-status={key} data-testid="sample-state" {...props} />;
}

const CHECK_LOOKS = {
  passed: { tone: "success", icon: CircleCheck },
  failed: { tone: "danger", icon: CircleX },
  none: { tone: "neutral", icon: CircleDashed },
} as const satisfies Record<CheckResult, StatusLook>;

/** The result of a merge's own UAT or PROD check: "UAT passed", "PROD failed", "UAT: no result". */
export function CheckBadge({ check, result, ...props }: StatusPillProps & { check: "uat" | "prod"; result: CheckResult }) {
  const t = useTranslations("mergeQueue.check");
  return (
    <StatusPill
      look={CHECK_LOOKS[result]}
      label={t(result, { check: t(check) })}
      data-check={check}
      data-status={result}
      data-testid="check-state"
      {...props}
    />
  );
}

export const SOURCE_ICONS: Record<MergeSource, LucideIcon> = {
  plan: ListChecks,
  task: Puzzle,
  builder: Hammer,
  label: TagIcon,
  revert: Undo2,
};

/** Where a merge request comes from, as a tag: a kind is a name, never a state. */
export function SourceTag({ source }: { source: string }) {
  const t = useTranslations("mergeQueue.source");
  const known = (MERGE_SOURCES as readonly string[]).includes(source) ? (source as MergeSource) : null;
  const Icon = known ? SOURCE_ICONS[known] : Ban;
  return (
    <Tag data-source={source} data-testid="merge-source">
      <Icon aria-hidden="true" />
      {known ? t(known) : source}
    </Tag>
  );
}
