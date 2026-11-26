"use client";

import type { Route } from "next";
import { useTranslations } from "next-intl";
import { type ReactNode, useId, useMemo } from "react";

import { useNow } from "@/hooks/use-now";
import { stepLabel } from "@/lib/plans";

import { AgentLanes } from "./agent-lanes";
import { type ProgressBoard, progressBoard, runSoFar, stepBoundaries, stepSpans } from "./progress-model";
import { ProgressStepCard } from "./progress-step-card";
import { isActiveState, type Run, type RunEvent } from "./queries";
import { usePlanProgress } from "./run-plan-steps";
import type { RunTask } from "./run-tasks";

/** Steps not started that show as a line each, before the rest fold into a count. */
const PENDING_SHOWN = 3;

/** "Run so far": a short paragraph from the board's figures. It is built, not written by a model. */
function RunSoFar({ board, tasks, working }: { board: ProgressBoard; tasks: readonly RunTask[]; working: boolean }) {
  const t = useTranslations("runs.detail.progress.so");
  const ids = useId();
  const so = runSoFar(board, tasks, working);
  const parts: string[] = [];
  if (so.total > 0) parts.push(t("steps", { done: so.done, total: so.total }));
  if (so.current) parts.push(so.current.title ? t("now", { step: so.current.key, title: so.current.title }) : t("nowBare", { step: so.current.key }));
  else if (so.total > 0 && so.done === so.total) parts.push(t("allDone"));
  if (so.agents > 0) parts.push(t("agents", { count: so.agents, running: so.agentsRunning }));
  if (so.agentsFailed > 0) parts.push(t("agentsFailed", { count: so.agentsFailed }));
  if (so.commits > 0) parts.push(t("commits", { count: so.commits }));
  return (
    <section className="flex min-w-0 flex-col gap-2 rounded-md border bg-card px-4 py-3 shadow-raised" aria-labelledby={`${ids}-title`} data-testid="run-so-far">
      <h2 id={`${ids}-title`} className="text-[15px] leading-[22px] font-semibold">
        {t("title")}
      </h2>
      <p className="max-w-[80ch] text-[15px] leading-6 text-foreground" data-testid="run-so-far-text">
        {parts.length > 0 ? parts.join(" ") : t("empty")}
      </p>
      <p className="text-xs leading-5 text-muted-foreground">{t("from")}</p>
    </section>
  );
}

/** Steps nobody started, folded into a few lines. */
function Pending({ board }: { board: ProgressBoard }) {
  const t = useTranslations("runs.detail.progress");
  if (board.pending.length === 0) return null;
  const shown = board.pending.slice(0, PENDING_SHOWN);
  const rest = board.pending.length - shown.length;
  return (
    <section className="flex min-w-0 flex-col gap-1 rounded-md border border-dashed px-4 py-3" aria-label={t("pending.title")} data-testid="progress-pending">
      <h3 className="text-xs font-semibold tracking-wide text-muted-foreground uppercase">{t("pending.title")}</h3>
      <ul className="flex flex-col gap-1">
        {shown.map((step) => (
          <li key={step.key} className="flex min-w-0 items-baseline gap-2 text-sm leading-5 text-muted-foreground" data-testid="progress-pending-step">
            <span className="w-5 shrink-0 font-mono text-xs text-fg-subtle tabular-nums">{step.key}</span>
            <span className="min-w-0 truncate">{stepLabel(step) || t("card.untitled")}</span>
          </li>
        ))}
      </ul>
      {rest > 0 ? <p className="text-sm leading-5 text-muted-foreground">{t("pending.more", { count: rest })}</p> : null}
    </section>
  );
}

/**
 * The run step by step, newest first: a paragraph of what the run has done so far, the lanes of every agent over the
 * whole run with a tick where each step ended, a card for every step with something to show (the one the run works on
 * is the card of Step N so far), and the steps not started folded into a few lines. Built from the plan the page holds
 * and the run's events: no model call, no request of its own.
 */
export function ProgressView({
  run,
  events,
  tasks,
  working,
  onOpenAgent,
  onOpenMain,
  onShowCall,
  onLive,
  stepHref,
  composer,
}: {
  run: Run;
  events: readonly RunEvent[];
  tasks: readonly RunTask[];
  working: boolean;
  onOpenAgent: (id: string) => void;
  onOpenMain: () => void;
  onShowCall: (call: string) => void;
  onLive: () => void;
  stepHref: (key: string) => Route;
  /** The box that messages the main agent, pinned to the bottom of the window under the cards; null when the visitor may not. */
  composer: ReactNode;
}) {
  const t = useTranslations("runs.detail.progress");
  const { view, current, pending: loading } = usePlanProgress(run);
  const active = isActiveState(run.state);
  const now = useNow(working);
  const running = active ? (run.step_key ?? current?.key ?? null) : null;
  const spans = useMemo(() => stepSpans(events, running, now), [events, running, now]);
  const board = useMemo(() => progressBoard({ steps: view?.steps ?? [], running, spans, tasks }), [view, running, spans, tasks]);
  const marks = useMemo(() => stepBoundaries(events).map((boundary) => ({ key: boundary.key, at: boundary.at })), [events]);
  return (
    <div className="flex min-w-0 flex-col gap-4" data-testid="progress-view" data-steps={board.total}>
      <RunSoFar board={board} tasks={tasks} working={working} />
      <AgentLanes events={events} tasks={tasks} working={working} onShow={onShowCall} onOpen={onOpenAgent} onOpenMain={onOpenMain} whole={{ steps: marks }} />
      {loading ? (
        <p className="text-sm text-muted-foreground" role="status">
          {t("loading")}
        </p>
      ) : !view ? (
        <p className="text-sm text-muted-foreground" data-testid="progress-no-plan">
          {t("noPlan")}
        </p>
      ) : (
        <>
          <ol aria-label={t("steps")} className="flex min-w-0 flex-col gap-3" data-testid="progress-steps">
            {board.cards.map((card) => (
              <ProgressStepCard
                key={card.step.key}
                run={run}
                card={card}
                view={view}
                events={events}
                tasks={tasks}
                onOpenAgent={onOpenAgent}
                onShowCall={onShowCall}
                onLive={onLive}
                stepHref={stepHref}
              />
            ))}
          </ol>
          <Pending board={board} />
        </>
      )}
      {composer ? (
        <div className="sticky bottom-2 z-10 rounded-lg border bg-background pb-[env(safe-area-inset-bottom)] shadow-raised" data-testid="progress-composer">
          {composer}
        </div>
      ) : null}
    </div>
  );
}
