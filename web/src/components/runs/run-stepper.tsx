"use client";

import {
  Ban,
  Circle,
  CircleCheck,
  CirclePause,
  CircleX,
  Clock,
  Eye,
  Loader2,
  type LucideIcon,
  MessageCircleQuestionMark,
  SquareTerminal,
  TimerOff,
} from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";

import { useStatusText } from "@/components/status/status-badge";
import { cn } from "@/lib/utils";

import type { RunState } from "./queries";
import type { Phase, PhaseItem, Stepper } from "./run-model";

const ENDED_ICON: Record<NonNullable<Stepper["ended"]>, LucideIcon> = { failed: CircleX, lost: TimerOff, cancelled: Ban };
const WAITING_ICON: Partial<Record<Phase, LucideIcon>> = { queued: Clock, review: Eye };
/** States the running phase shows under their own name and icon: a person drives it, or a plan run waits or is parked. */
const RUNNING_AS: Partial<Record<RunState, LucideIcon>> = {
  interactive: SquareTerminal,
  waiting: MessageCircleQuestionMark,
  parked: CirclePause,
};

const BAR: Record<PhaseItem["status"], string> = {
  done: "bg-success-solid",
  current: "bg-running",
  stopped: "bg-destructive",
  todo: "bg-border",
};

function Item({ item, ended, state }: { item: PhaseItem; ended: Stepper["ended"]; state: RunState }) {
  const t = useTranslations("runs.detail.stepper");
  const tState = useStatusText("run");
  const format = useFormatter();
  const own = item.phase === "running" && item.status === "current" ? (RUNNING_AS[state] ?? null) : null;
  const label = item.status === "stopped" && ended ? tState(ended) : own ? tState(state) : t(`phase.${item.phase}`);
  let Icon: LucideIcon = Circle;
  let spin = false;
  if (item.status === "done") Icon = CircleCheck;
  else if (item.status === "stopped" && ended) Icon = ENDED_ICON[ended];
  else if (item.status === "current") {
    Icon = own ?? WAITING_ICON[item.phase] ?? Loader2;
    spin = Icon === Loader2;
  }
  return (
    <li
      className="flex min-w-0 flex-col gap-1.5"
      aria-current={item.status === "current" ? "step" : undefined}
      data-phase={item.phase}
      data-status={item.status}
      data-testid="run-phase"
    >
      <span className={cn("h-1 rounded-full", BAR[item.status])} aria-hidden="true" />
      <span
        className={cn(
          "flex min-w-0 items-center gap-1.5 text-sm",
          item.status === "todo" ? "text-muted-foreground" : "font-medium",
          item.status === "stopped" && "text-danger",
        )}
      >
        <Icon
          className={cn(
            "size-4 shrink-0",
            item.status === "done" && "text-success",
            item.status === "current" && "text-running",
            spin && "animate-spin motion-reduce:animate-none",
          )}
          aria-hidden="true"
        />
        <span className="truncate">{label}</span>
        <span className="sr-only">, {t(`status.${item.status}`)}</span>
      </span>
      <span className="min-h-4 font-mono text-xs text-muted-foreground tabular-nums">
        {item.status === "stopped" ? (
          <span className="sr-only">{t("during", { phase: t(`phase.${item.phase}`) })} </span>
        ) : null}
        {item.at ? (
          <time dateTime={item.at} title={format.dateTime(new Date(item.at), { dateStyle: "medium", timeStyle: "medium" })}>
            {format.dateTime(new Date(item.at), { timeStyle: "medium" })}
          </time>
        ) : null}
      </span>
    </li>
  );
}

/**
 * The states a run goes through, left to right, with the time it entered each one. The current state carries
 * aria-current="step" and an icon; a run that ended badly says how on the state it ended in. Colour is never the only
 * cue: each item has an icon and its status in words for screen readers.
 */
export function RunStepper({ stepper, state }: { stepper: Stepper; state: RunState }) {
  const t = useTranslations("runs.detail.stepper");
  return (
    <section aria-label={t("label")} className="rounded-md border bg-card shadow-raised px-4 py-4" data-testid="run-stepper">
      <ol className="grid grid-cols-3 gap-x-3 gap-y-4 md:grid-flow-col md:auto-cols-fr md:grid-cols-none">
        {stepper.items.map((item) => (
          <Item key={item.phase} item={item} ended={stepper.ended} state={state} />
        ))}
      </ol>
    </section>
  );
}
