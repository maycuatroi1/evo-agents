"use client";

import {
  CircleCheck,
  CircleMinus,
  CirclePause,
  CirclePlay,
  CircleX,
  Clock,
  Hourglass,
  OctagonPause,
  PackageCheck,
  TimerOff,
  UserX,
} from "lucide-react";
import { useTranslations } from "next-intl";
import type { ReactNode } from "react";

import {
  LookIcon,
  OtherStatusBadge,
  type RunStatus,
  STATUS_LOOKS,
  StatusBadge,
  type StatusLook,
  StatusPill,
  type StatusPillProps,
} from "@/components/status/status-badge";

import type { CheckOutcome, CiState, CycleState } from "./queries";

/**
 * The states of the Releases page as pills: the CI of a tag a cycle follows, a cycle, and the outcome of a check. Only
 * this page shows them, so their looks live here rather than in `STATUS_LOOKS`, which nearly every page loads; each is
 * the shared `StatusPill`, with its words under `releases` in messages/en.json and vi.json.
 */

/** The CI of a tag (`release_ci.CI_STATES`): running while its workflow runs, red or timed out in `danger`. */
export const CI_LOOKS = {
  pending: { tone: "neutral", icon: Clock },
  running: { tone: "running", icon: CirclePlay, dot: "live" },
  passed: { tone: "success", icon: CircleCheck },
  failed: { tone: "danger", icon: CircleX },
  timeout: { tone: "danger", icon: TimerOff },
} as const satisfies Record<CiState, StatusLook>;

/** A cycle (`releases.CYCLE_STATES`): stopped waits for its owner's answer, published shipped a version. */
export const CYCLE_LOOKS = {
  open: { tone: "neutral", icon: CirclePlay },
  stopped: { tone: "attention", icon: OctagonPause },
  published: { tone: "success", icon: PackageCheck },
  closed: { tone: "outline", icon: CircleMinus },
} as const satisfies Record<CycleState, StatusLook>;

/** A check (`releases.CHECK_OUTCOMES`): a pause and an owner without the admin role need a person, an error failed. */
export const OUTCOME_LOOKS = {
  opened: { tone: "neutral", icon: CirclePlay },
  unchanged: { tone: "neutral", icon: CircleMinus },
  busy: { tone: "neutral", icon: Hourglass },
  paused: { tone: "attention", icon: CirclePause },
  owner: { tone: "attention", icon: UserX },
  error: { tone: "danger", icon: CircleX },
} as const satisfies Record<CheckOutcome, StatusLook>;

/** The schedule paused, as the pill beside the page's title says it. */
export const PAUSED_LOOK: StatusLook = { tone: "attention", icon: CirclePause };

export function CiBadge({ state, ...props }: StatusPillProps & { state: CiState }) {
  const t = useTranslations("releases.ci");
  return <StatusPill look={CI_LOOKS[state]} label={t(state)} data-kind="ci" data-status={state} data-testid="release-ci" {...props} />;
}

export function CycleBadge({ state, label, ...props }: StatusPillProps & { state: CycleState; label?: ReactNode }) {
  const t = useTranslations("releases.cycleState");
  return (
    <StatusPill look={CYCLE_LOOKS[state]} label={label ?? t(state)} data-kind="cycle" data-status={state} data-testid="release-cycle-state" {...props} />
  );
}

/** A tag's CI as its icon alone, in its tone, with its word for screen readers and as a tooltip: for dense lists. */
export function CiIcon({ state, className }: { state: CiState; className?: string }) {
  const t = useTranslations("releases.ci");
  return <LookIcon look={CI_LOOKS[state]} label={t(state)} className={className} data-status={state} />;
}

/** A check's outcome as its icon and its words, in its tone. */
export function OutcomeLine({ outcome, children, testId }: { outcome: CheckOutcome; children?: ReactNode; testId?: string }) {
  const t = useTranslations("releases.outcome");
  const look = OUTCOME_LOOKS[outcome];
  const tone = look.tone === "danger" ? "text-danger" : look.tone === "attention" ? "text-attention" : "text-foreground";
  return (
    <span className={`inline-flex min-w-0 items-start gap-1.5 ${tone}`} data-outcome={outcome} data-testid={testId}>
      <look.icon className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
      <span className="min-w-0 font-medium">{children ?? t(outcome)}</span>
    </span>
  );
}

function isRunStatus(state: string): state is RunStatus {
  return Object.hasOwn(STATUS_LOOKS.run, state);
}

/** A run of a cycle in its state, as every run's pill; a state the web does not know shows as written. */
export function RunStateBadge({ state }: { state: string }) {
  return isRunStatus(state) ? <StatusBadge kind="run" status={state} /> : <OtherStatusBadge raw={state} />;
}
