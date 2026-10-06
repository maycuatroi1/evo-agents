"use client";

import {
  Ban,
  CircleCheck,
  CirclePause,
  CircleX,
  Clock,
  Eye,
  Hand,
  ListChecks,
  Loader2,
  type LucideIcon,
  MessageCircleQuestionMark,
  SquareTerminal,
  TimerOff,
  Workflow,
} from "lucide-react";
import { useTranslations } from "next-intl";

import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

import type { PlanRunPhase } from "./model";
import type { RunState } from "./queries";

type Variant = "success" | "info" | "warning" | "destructive" | "secondary";

/** Each state's icon and colour; the word is always there too, so colour is never the only signal. */
export const STATE_LOOK: Record<RunState, { icon: LucideIcon; variant: Variant; spin?: boolean }> = {
  queued: { icon: Clock, variant: "warning" },
  leased: { icon: Hand, variant: "info" },
  running: { icon: Loader2, variant: "info", spin: true },
  interactive: { icon: SquareTerminal, variant: "warning" },
  verifying: { icon: ListChecks, variant: "info" },
  waiting: { icon: MessageCircleQuestionMark, variant: "warning" },
  review: { icon: Eye, variant: "warning" },
  parked: { icon: CirclePause, variant: "secondary" },
  done: { icon: CircleCheck, variant: "success" },
  failed: { icon: CircleX, variant: "destructive" },
  lost: { icon: TimerOff, variant: "secondary" },
  cancelled: { icon: Ban, variant: "secondary" },
};

export function RunStateBadge({ state, className }: { state: RunState; className?: string }) {
  const t = useTranslations("runs.state");
  const { icon: Icon, variant, spin } = STATE_LOOK[state];
  return (
    <Badge variant={variant} className={className} data-state={state} data-testid="run-state">
      <Icon className={cn(spin && "animate-spin motion-reduce:animate-none")} aria-hidden="true" />
      {t(state)}
    </Badge>
  );
}

/** A plan run's state as the plan pages say it: Queued, Running (whatever the worker is doing), Waiting or Parked. */
export const PHASE_LOOK: Record<PlanRunPhase, { icon: LucideIcon; variant: Variant; spin?: boolean }> = {
  queued: STATE_LOOK.queued,
  running: STATE_LOOK.running,
  waiting: STATE_LOOK.waiting,
  parked: STATE_LOOK.parked,
  review: STATE_LOOK.review,
};

export function PlanRunPhaseBadge({ phase, long = false, className }: { phase: PlanRunPhase; long?: boolean; className?: string }) {
  const t = useTranslations("runs.planRun.phase");
  const { icon: Icon, variant, spin } = PHASE_LOOK[phase];
  return (
    <Badge variant={variant} className={className} data-phase={phase} data-testid="plan-run-phase">
      <Icon className={cn(spin && "animate-spin motion-reduce:animate-none")} aria-hidden="true" />
      {long ? t(`${phase}Long`) : t(phase)}
    </Badge>
  );
}

/** "Plan run": what tells a run of a whole plan from a run of one step, in tables and on the run page. */
export function PlanRunKindBadge({ className }: { className?: string }) {
  const t = useTranslations("runs.planRun");
  return (
    <Badge variant="outline" className={className} data-testid="run-kind" data-kind="plan">
      <Workflow aria-hidden="true" />
      {t("kind")}
    </Badge>
  );
}
