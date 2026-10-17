"use client";

import { useFormatter, useTranslations } from "next-intl";
import type { CSSProperties } from "react";

import { useNow } from "@/components/kg/use-now";
import { useStatusText } from "@/components/status/status-badge";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";

import type { RunMove } from "./log-model";
import { isActiveState, type Run } from "./queries";
import { type PhaseTone, type TimelinePhase, timelineModel } from "./run-model";
import { durationParts } from "./trace-model";

/**
 * The kit's RunTimeline: a run's phases left to right, with the time spent between each and the next on the line that
 * joins them, so a slow phase shows at a glance. Under 768 px it stays one row, about a third as tall as a list: the
 * names in 12 px under the nodes, two lines at most, the time each took under its name, and no clock times (the trace
 * and Details have them). Done phases are `fg-muted` nodes on a
 * solid line; the current one pulses in `running` with a dashed line ahead and a gap that counts up; a run waiting for
 * an answer, or parked, is an `attention` node with its state and since when; a failed run stops on a `danger` node,
 * the phases after it skipped; only a run that is done ends on a `success` node. Times are local, in `code-small`, with
 * the full date and time zone in a tooltip. An ordered list, the current phase `aria-current="step"`, each phase's
 * status in words for screen readers.
 */

const NODE: Record<PhaseTone, string> = {
  done: "border-muted-foreground bg-muted-foreground",
  current: "border-running bg-running text-running",
  queued: "border-neutral-solid bg-card",
  waiting: "border-attention-solid bg-attention-solid",
  review: "border-review-solid bg-review-solid",
  success: "border-success-solid bg-success-solid",
  failed: "border-danger-solid bg-danger-solid",
  ended: "border-neutral-solid bg-neutral-solid",
  todo: "border-border-strong bg-card",
};

const DASHED = "bg-[repeating-linear-gradient(90deg,var(--border-strong)_0_4px,transparent_4px_8px)]";

/** "0s", "48s", "8m 7s", "20m", "1h 5m": the time a phase took. */
export function useGap() {
  const t = useTranslations("runs.detail.timeline.gap");
  return (ms: number) => {
    const { hours, minutes, seconds } = durationParts(ms);
    if (hours > 0) return t("hours", { hours, minutes });
    if (minutes > 0) return seconds > 0 ? t("minutes", { minutes, seconds }) : t("minutesOnly", { minutes });
    return t("seconds", { seconds });
  };
}

/** A local time in `code-small`, its full date and time zone in a tooltip that keyboard focus opens too. */
function PhaseTime({ at, prefix }: { at: string; prefix?: (time: string) => string }) {
  const format = useFormatter();
  const date = new Date(at);
  const short = format.dateTime(date, { timeStyle: "medium" });
  const full = format.dateTime(date, { dateStyle: "full", timeStyle: "long" });
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <time dateTime={at} tabIndex={0} className="rounded-xs tabular-nums" data-testid="run-phase-time">
          {prefix ? prefix(short) : short}
        </time>
      </TooltipTrigger>
      <TooltipContent side="bottom">{full}</TooltipContent>
    </Tooltip>
  );
}

function Phase({ phase, last, ended }: { phase: TimelinePhase; last: boolean; ended: boolean }) {
  const t = useTranslations("runs.detail.timeline");
  const tState = useStatusText("run");
  const gap = useGap();
  const current = phase.status === "current";
  const name = phase.state ? tState(phase.state) : t(`phase.${phase.phase}`);
  const dashedAhead = current || phase.status === "todo" || phase.status === "stopped";
  const gapText =
    phase.gapMs === null ? null : phase.tone === "waiting" ? t("waitingGap", { duration: gap(phase.gapMs) }) : gap(phase.gapMs);

  return (
    <li
      className="relative min-w-0 pt-[22px] pr-2 max-md:pr-1"
      aria-current={current ? "step" : undefined}
      data-phase={phase.phase}
      data-status={phase.status}
      data-tone={phase.tone}
      data-testid="run-phase"
    >
      {last ? null : (
        <span
          aria-hidden="true"
          className={cn(
            "absolute top-[6px] right-1 left-[18px] h-0.5 rounded-[1px]",
            dashedAhead ? DASHED : "bg-fg-subtle",
          )}
        />
      )}
      <span aria-hidden="true" className={cn("absolute top-0 left-0 size-3.5 rounded-full border-2", NODE[phase.tone])}>
        {phase.tone === "current" ? <span className="absolute -inset-0.5 animate-live-ping rounded-full bg-current" /> : null}
      </span>
      <div
        className={cn(
          "truncate text-[13px] leading-[18px] max-md:line-clamp-2 max-md:text-xs max-md:leading-4 max-md:whitespace-normal max-md:[overflow-wrap:anywhere]",
          phase.status === "todo" ? "text-fg-subtle" : "font-medium text-foreground",
          phase.tone === "waiting" && "text-attention",
          phase.tone === "failed" && "text-danger",
        )}
        title={name}
        data-testid="run-phase-name"
      >
        {name}
        <span className="sr-only">, {t(`status.${phase.status}`)}</span>
      </div>
      <div className="min-h-4 truncate font-mono text-xs leading-4 text-fg-subtle max-md:hidden">
        {phase.tone === "waiting" && phase.since ? (
          <PhaseTime at={phase.since} prefix={(time) => t("since", { time })} />
        ) : phase.at && phase.status !== "todo" ? (
          <PhaseTime at={phase.at} />
        ) : phase.status === "todo" ? (
          ended ? t("skipped") : t("notYet")
        ) : null}
      </div>
      {gapText && !last ? (
        <span
          className={cn(
            "mt-0.5 block font-mono text-[11px] leading-4 text-muted-foreground tabular-nums md:absolute md:-top-px md:left-[calc(50%+4px)] md:mt-0 md:-translate-x-1/2 md:bg-card md:px-1 md:whitespace-nowrap",
            phase.tone === "current" && "text-running",
            phase.tone === "waiting" && "text-attention",
          )}
          data-testid="run-phase-gap"
        >
          <span className="sr-only">{t("gapLabel")} </span>
          {gapText}
        </span>
      ) : null}
    </li>
  );
}

/** `bare`: no card of its own, for the run at a glance on a phone, which holds it. */
export function RunTimeline({ run, moves, bare = false }: { run: Run; moves: readonly RunMove[]; bare?: boolean }) {
  const t = useTranslations("runs.detail.timeline");
  const now = useNow(isActiveState(run.state));
  const timeline = timelineModel(run, moves, now);
  const count = timeline.phases.length;
  return (
    <section
      aria-label={t("section")}
      className={bare ? "px-4 py-3" : "rounded-md border bg-card px-4 py-4 shadow-raised max-md:px-3 max-md:py-3"}
      data-testid="run-timeline"
    >
      <ol
        aria-label={t("label", { id: run.id })}
        className="grid grid-cols-[repeat(var(--phases),minmax(0,1fr))]"
        style={{ "--phases": count } as CSSProperties}
      >
        {timeline.phases.map((phase, index) => (
          <Phase key={phase.phase} phase={phase} last={index === count - 1} ended={timeline.ended !== null} />
        ))}
      </ol>
    </section>
  );
}
