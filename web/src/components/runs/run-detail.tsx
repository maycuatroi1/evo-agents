"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useTranslations } from "next-intl";
import { type ReactNode, useCallback, useEffect, useId, useState } from "react";

import { useLiveSignal } from "@/components/live/live-context";
import { planHref, stepHref } from "@/components/plans/links";
import { PageHeader } from "@/components/shell/page-header";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { NotFoundState, PageSkeleton } from "@/components/states/states";
import { StatusBadge, useStatusText } from "@/components/status/status-badge";
import { workerQuery } from "@/components/workers/queries";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";
import { whoamiQuery } from "@/lib/queries";

import { PlanRunKindBadge } from "./badges";
import { useRunViewer } from "./hooks";
import type { RunMove } from "./log-model";
import { isActiveState, type Run, RUN_VIEW_PARAM, runKey, runQuery } from "./queries";
import { RunActions, RunNotes } from "./run-actions";
import { RunComposer } from "./run-composer";
import { RunDetails, RunResult } from "./run-facts";
import { RunLogCard, type SessionTab } from "./run-log";
import { runControls, stepperModel } from "./run-model";
import { RunDecisions } from "./run-decisions";
import { RunPlanSteps } from "./run-plan-steps";
import { RunStepper } from "./run-stepper";
import { RunTerminalPanel } from "./run-terminal";
import { terminalAccess } from "./terminal-model";
import { logSignal, useRunLog } from "./use-run-log";

/** A move between states as the log says it: "Running to Verifying, by the worker: ..." */
function useDescribeMove() {
  const t = useTranslations("runs.detail.log");
  const tState = useStatusText("run");
  return useCallback(
    (move: RunMove) => {
      const to = tState(move.to);
      const actor = move.actor === "worker" || move.actor === "owner" || move.actor === "reaper" ? t(`actor.${move.actor}`) : move.actor;
      const from = move.from ? tState(move.from) : null;
      const text = from ? (actor ? t("moveBy", { from, to, actor }) : t("move", { from, to })) : t("moveTo", { to });
      return move.reason ? t("moveReason", { move: text, reason: move.reason }) : text;
    },
    [t, tState],
  );
}

/**
 * The Terminal tab, for the run's owner on a worker of theirs that allows the web terminal; null for anyone else. The
 * worker is read only for the run's owner (the API answers its owner and hub admins). Once offered, the tab stays while
 * the page is open, so a session that ended with the run still shows why.
 */
function useRunTerminalTab(run: Run, viewer: ReturnType<typeof useRunViewer>) {
  const ownsRun = viewer !== null && viewer.login === run.dispatched_by;
  const { data: worker } = useQuery({
    ...workerQuery(browserApi, run.worker_id ?? 0),
    enabled: ownsRun && run.worker_id !== null,
    refetchInterval: false, // its owner and whether it allows the terminal are fixed when it registers
  });
  const { data: me } = useQuery(whoamiQuery(browserApi));
  const access = terminalAccess(run, viewer, worker);
  const [offered, setOffered] = useState(false);
  if (access?.open && !offered) setOffered(true);
  if (access === null || !(access.open || offered)) return null;
  return { open: access.open, sessionCreatedAt: me?.token.kind === "web" ? me.token.created_at : null };
}

/**
 * The tab the log card shows, held by the page: the Terminal tab once offered when the page was opened on it
 * (`?view=terminal`, a decision's Take over from the Inbox), or when a decision's Take over here asks for it.
 */
function useSessionTab(terminalOffered: boolean) {
  const params = useSearchParams();
  const wanted = params.get(RUN_VIEW_PARAM) === "terminal";
  const [tab, setTab] = useState<SessionTab>("log");
  const [applied, setApplied] = useState(false);
  if (wanted && terminalOffered && !applied) {
    setApplied(true);
    setTab("terminal");
  }
  return [tab, setTab] as const;
}

function RunPage({ run }: { run: Run }) {
  const t = useTranslations("runs.detail");
  const queryClient = useQueryClient();
  const viewer = useRunViewer(run.project);
  const controls = runControls(run, viewer);
  const terminal = useRunTerminalTab(run, viewer);
  const [sessionTab, setSessionTab] = useSessionTab(terminal !== null);
  const logId = useId();
  const takeOver = terminal?.open
    ? () => {
        setSessionTab("terminal");
        const card = document.getElementById(logId);
        card?.scrollIntoView({ block: "start", behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
        card?.querySelector<HTMLElement>('[data-testid="run-tab-terminal"]')?.focus({ preventScroll: true });
      }
    : undefined;
  const describe = useDescribeMove();
  const log = useRunLog({ project: run.project, runId: run.id, knownLastSeq: run.last_seq, describe });
  const [frozenAt, setFrozenAt] = useState<number | null>(null);
  // While the stream (or its fallback) carries the run, it is what the top bar's LiveIndicator follows; before it opens
  // and once it ended, the run's own query (every 5 seconds while active) does.
  useLiveSignal(logSignal(log, frozenAt !== null), { resume: () => setFrozenAt(null) });

  // A move the log tells, or the end of the stream, changes the run: read it again at once rather than at the next tick.
  const moves = log.moves.length;
  const ended = log.status === "ended";
  useEffect(() => {
    if (moves > 0 || ended) void queryClient.invalidateQueries({ queryKey: runKey(run.project, run.id) });
  }, [moves, ended, queryClient, run.project, run.id]);

  const stepper = stepperModel(run, log.moves);
  const plan = run.kind === "plan";
  const title = run.title ?? (plan ? run.plan_id : t("untitled"));
  const planLink = (chunks: ReactNode) => (
    <Link href={planHref(run.project, run.plan_id)} className="font-mono text-brand underline-offset-4 hover:underline" data-testid="run-plan-link">
      {chunks}
    </Link>
  );

  return (
    <div className="flex flex-col gap-6" data-testid="run-detail" data-run-id={run.id} data-state={run.state} data-kind={run.kind}>
      <PageHeader
        title={
          <>
            {plan ? t("planTitle") : t("title")} <span className="tabular-nums">#{run.id}</span>
          </>
        }
        status={<StatusBadge kind="run" status={run.state} size="lg" />}
        tags={plan ? <PlanRunKindBadge /> : null}
        sub={
          plan || run.step_key === null
            ? t.rich("planSub", { plan: run.plan_id, title, planLink })
            : t.rich("sub", {
                plan: run.plan_id,
                step: run.step_key,
                title,
                planLink,
                stepLink: (chunks) => (
                  <Link
                    href={stepHref(run.project, run.plan_id, run.step_key ?? "")}
                    className="text-brand underline-offset-4 hover:underline"
                    data-testid="run-step-link"
                  >
                    {chunks}
                  </Link>
                ),
              })
        }
        actions={<RunActions run={run} controls={controls} />}
      />
      <RunNotes run={run} controls={controls} />
      <RunStepper stepper={stepper} state={run.state} />
      {/* The kit's run screen: the log on the left, the decision, details and result in the side column from xl. Below
          xl one column, the decision first. */}
      <div className="grid items-start gap-4 xl:grid-cols-[minmax(0,1fr)_22rem]">
        {plan ? <RunDecisions run={run} owner={controls.owner} onTakeOver={takeOver} className="xl:col-start-2 xl:row-start-1" /> : null}
        <div id={logId} className="min-w-0 scroll-mt-16 xl:col-start-1 xl:row-span-2 xl:row-start-1">
          <RunLogCard
            runId={run.id}
            log={log}
            frozen={{ at: frozenAt, set: setFrozenAt }}
            active={isActiveState(run.state)}
            composer={controls.message ? <RunComposer run={run} /> : null}
            tab={sessionTab}
            onTabChange={setSessionTab}
            terminal={
              terminal
                ? (shown) => (
                    <RunTerminalPanel run={run} open={terminal.open} active={shown} sessionCreatedAt={terminal.sessionCreatedAt} />
                  )
                : null
            }
          />
        </div>
        <div className="flex min-w-0 flex-col gap-4 xl:col-start-2">
          <RunDetails run={run} viewer={viewer} />
          {plan ? <RunPlanSteps run={run} /> : null}
          <RunResult run={run} />
        </div>
      </div>
    </div>
  );
}

/**
 * One run: its state as a stepper, its live log, the owner's controls (cancel, take over, hand back, approve, rerun,
 * a message to the agent), its details and its result. A plan run also lists its plan's steps with their status, and
 * shows the decisions it waits on with the owner's answer form at the top of the side column. A run of a plan the visitor may not read, or of another
 * project, is not found.
 */
export function RunDetail({ project, runId, initialError }: { project: string; runId: number; initialError: ApiErrorInfo | null }) {
  const t = useTranslations("runs.detail");
  const state = useHubQuery(runQuery(browserApi, project, runId), initialError, { live: true });
  if (state.status === "error" && state.error.status === 404) {
    return <NotFoundState title={t("notFoundTitle", { id: runId })} description={t("notFoundDescription")} />;
  }
  return (
    <QueryView state={state} loading={<PageSkeleton />}>
      {(run) => <RunPage run={run} />}
    </QueryView>
  );
}
