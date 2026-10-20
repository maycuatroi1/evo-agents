"use client";

import { ChevronRight } from "lucide-react";
import type { Route } from "next";
import Link from "next/link";
import { useTranslations } from "next-intl";
import type { ReactNode } from "react";

import { useNow } from "@/hooks/use-now";
import { percent, stepLabel } from "@/lib/plans";
import { useRuntimeName } from "@/lib/runtimes";
import { cn } from "@/lib/utils";

import { isSessionTurn } from "./chat-session";
import { ChatSessionHead } from "./chat-session-turns";
import { runTiming } from "./model";
import { isActiveState, type Run } from "./queries";
import { usePlanProgress } from "./run-plan-steps";
import { useGap } from "./run-timeline";

/** How long the run has been in its state, for the status line: since it was queued, began waiting, or ran. */
function stateDuration(run: Run, now: number | null): number | null {
  if (run.state === "waiting" && run.waiting_since && now !== null) return Math.max(0, now - Date.parse(run.waiting_since));
  if (run.state === "parked" && run.parked_at && now !== null) return Math.max(0, now - Date.parse(run.parked_at));
  return runTiming(run, now).durationMs;
}

/**
 * The run's status line, one sentence as GitHub's "succeeded 4 hours ago in 3m 53s": the state, how long, and where it
 * runs ("Running for 42m 10s on binhna-macbook-m4"), the attempt after it once there was more than one. It ticks every
 * second while the run is active.
 */
export function useStatusLine(run: Run): string {
  const t = useTranslations("runs.detail.header");
  const tRuntime = useRuntimeName();
  const gap = useGap();
  const now = useNow(isActiveState(run.state));
  const ms = stateDuration(run, now);
  const duration = ms !== null ? gap(ms) : run.run_seconds > 0 ? gap(run.run_seconds * 1000) : "0s";
  const sentence = t(`state.${run.state}`, { duration, worker: run.worker ?? t("worker"), runtime: tRuntime(run.runtime) });
  return run.attempt > 1 ? t("attempt", { sentence, attempt: run.attempt, max: run.max_attempts }) : sentence;
}

/**
 * The head of a run's page, one line over the trace, which is what the page is for: the plan and its step in progress
 * as links (or, for another kind of run, where it comes from), the plan's progress as a 64 px bar and "6/21 steps", and
 * the status line when the state is not the usual one ("Waiting for an answer for 6m", "Failed after 1h 12m"; a run
 * that is running or verifying says it to screen readers only), 16 px apart, wrapping as the width needs; at its
 * end the Details toggle, the owner's one or two controls and the menu of the others. "Run #80" is the page's heading for screen readers only: the top bar's
 * breadcrumb shows it. A turn of a chat session has its session's title above that line, and the session's other turns.
 */
export function RunHeader({
  run,
  sub,
  planHref,
  stepHref,
  details,
  buttons,
  menu,
}: {
  run: Run;
  sub: ReactNode;
  /** The plan's page and a step's, which the page knows how to reach. */
  planHref: Route;
  stepHref: (key: string) => Route;
  details: ReactNode;
  buttons: ReactNode;
  menu: ReactNode;
}) {
  const t = useTranslations("runs.detail.header");
  const tRun = useTranslations("runs.detail");
  const line = useStatusLine(run);
  const plan = run.kind === "plan";
  const { pending, counts, current } = usePlanProgress(run);
  return (
    // The controls wrap under the line when the width leaves them no room, so the page never scrolls sideways.
    <header className="flex min-w-0 flex-wrap items-start gap-x-3 gap-y-2" data-testid="run-header">
      <h1 className="sr-only">
        {tRun("title")} #{run.id}
      </h1>
      {isSessionTurn(run) ? <ChatSessionHead run={run} /> : null}
      <div className="flex min-h-8 min-w-0 flex-1 basis-60 flex-wrap items-center gap-x-4 gap-y-1 text-[13px] leading-5 text-muted-foreground">
        {plan ? (
          <span className="inline-flex max-w-full min-w-0 items-center gap-1 max-md:items-start">
            <Link href={planHref} className="shrink-0 rounded-xs hover:text-foreground hover:underline" data-testid="run-plan-link">
              {run.plan_id}
            </Link>
            {current ? (
              <>
                <ChevronRight className="size-3.5 shrink-0 text-fg-subtle max-md:mt-[3px]" aria-hidden="true" />
                <Link
                  href={stepHref(current.key)}
                  className="min-w-0 truncate rounded-xs font-medium text-foreground hover:underline max-md:line-clamp-2 max-md:whitespace-normal"
                  title={stepLabel(current) || current.key}
                  data-testid="run-current-step"
                >
                  {t("step", { key: current.key, title: stepLabel(current) || current.key })}
                </Link>
              </>
            ) : pending ? (
              // The step's place while the plan is read, so the line does not grow under the reader.
              <span className="ml-1 block h-3.5 w-40 max-w-full animate-pulse rounded-md bg-muted" data-testid="run-header-loading" />
            ) : null}
          </span>
        ) : sub ? (
          <span className="min-w-0 [overflow-wrap:anywhere]" data-slot="page-sub">
            {sub}
          </span>
        ) : null}
        {plan && counts ? (
          <>
            <span className="inline-flex shrink-0 items-center gap-2 tabular-nums" data-testid="run-progress">
              <span
                role="progressbar"
                aria-valuemin={0}
                aria-valuemax={counts.total}
                aria-valuenow={counts.done}
                aria-label={t("progressLabel")}
                className="h-1 w-16 overflow-hidden rounded-full bg-muted"
              >
                <span
                  className={cn("block h-full rounded-full", run.state === "done" ? "bg-success-solid" : "bg-running")}
                  style={{ width: `${percent(counts.done, counts.total)}%` }}
                />
              </span>
              <span data-testid="run-progress-text">{t("progressShort", { done: counts.done, total: counts.total })}</span>
            </span>
          </>
        ) : pending ? (
          <span className="block h-3.5 w-24 animate-pulse rounded-md bg-muted" />
        ) : null}
        {/* A run going as it should (running, verifying) needs no words: the line says only what is not usual. */}
        <span className={cn("tabular-nums", STATE_TONE[run.state], (run.state === "running" || run.state === "verifying") && "sr-only")} data-testid="run-status-line">
          {line}
        </span>
      </div>
      <div className="ml-auto flex max-w-full min-w-0 flex-wrap items-center justify-end gap-1">
        {details}
        {buttons}
        {menu}
      </div>
    </header>
  );
}

/** The status line says the state: in its tone when it needs a person or went wrong, else in the line's grey. */
const STATE_TONE: Partial<Record<Run["state"], string>> = {
  waiting: "text-attention",
  parked: "text-attention",
  review: "text-attention",
  failed: "text-danger",
  lost: "text-danger",
};
