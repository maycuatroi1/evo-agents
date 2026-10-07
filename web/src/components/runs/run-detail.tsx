"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { type ReactNode, useCallback, useEffect, useState } from "react";

import { NoticeArea, useNotice } from "@/components/admin/notice";
import { planHref, stepHref } from "@/components/plans/links";
import { PageHeader } from "@/components/shell/page-header";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { NotFoundState, PageSkeleton } from "@/components/states/states";
import { workerQuery } from "@/components/workers/queries";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";
import { whoamiQuery } from "@/lib/queries";

import { PlanRunKindBadge, RunStateBadge } from "./badges";
import { useRunViewer } from "./hooks";
import type { RunMove } from "./log-model";
import { isActiveState, type Run, runKey, runQuery } from "./queries";
import { RunActions, RunNotes } from "./run-actions";
import { RunComposer } from "./run-composer";
import { RunCredentials } from "./run-credentials";
import { RunDetails, RunResult } from "./run-facts";
import { RunLogCard } from "./run-log";
import { runControls, stepperModel } from "./run-model";
import { RunDecisions } from "./run-decisions";
import { RunPlanSteps } from "./run-plan-steps";
import { RunStepper } from "./run-stepper";
import { RunTerminalPanel } from "./run-terminal";
import { terminalAccess } from "./terminal-model";
import { useRunLog } from "./use-run-log";

/** A move between states as the log says it: "Running to Verifying, by the worker: ..." */
function useDescribeMove() {
  const t = useTranslations("runs.detail.log");
  const tState = useTranslations("runs.state");
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

function RunPage({ run }: { run: Run }) {
  const t = useTranslations("runs.detail");
  const queryClient = useQueryClient();
  const viewer = useRunViewer(run.project);
  const controls = runControls(run, viewer);
  const terminal = useRunTerminalTab(run, viewer);
  const describe = useDescribeMove();
  const { notice, show, clear } = useNotice();
  const log = useRunLog({ project: run.project, runId: run.id, knownLastSeq: run.last_seq, describe });

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
    <Link href={planHref(run.project, run.plan_id)} className="font-mono text-primary underline-offset-4 hover:underline" data-testid="run-plan-link">
      {chunks}
    </Link>
  );

  return (
    <div className="flex flex-col gap-6" data-testid="run-detail" data-run-id={run.id} data-state={run.state} data-kind={run.kind}>
      <PageHeader
        eyebrow={<span className="font-mono normal-case">{run.project}</span>}
        title={
          <span className="flex flex-wrap items-center gap-x-3 gap-y-1.5">
            <span>
              {plan ? t("planTitle") : t("title")} <span className="font-mono tabular-nums">#{run.id}</span>
            </span>
            <RunStateBadge state={run.state} className="h-6 text-sm" />
            {plan ? <PlanRunKindBadge className="h-6 text-sm" /> : null}
          </span>
        }
        description={
          plan || run.step_key === null
            ? t.rich("planDescription", { plan: run.plan_id, title, planLink })
            : t.rich("description", {
                plan: run.plan_id,
                step: run.step_key,
                title,
                planLink,
                stepLink: (chunks) => (
                  <Link
                    href={stepHref(run.project, run.plan_id, run.step_key ?? "")}
                    className="text-primary underline-offset-4 hover:underline"
                    data-testid="run-step-link"
                  >
                    {chunks}
                  </Link>
                ),
              })
        }
        meta={<RunActions run={run} controls={controls} onNotice={show} />}
        metaBelow
      />
      <NoticeArea notice={notice} onDismiss={clear} />
      {plan ? <RunDecisions run={run} owner={controls.owner} /> : null}
      <RunNotes run={run} controls={controls} />
      <RunStepper stepper={stepper} state={run.state} />
      <div className="grid items-start gap-4 xl:grid-cols-[minmax(0,1fr)_22rem]">
        <RunLogCard
          runId={run.id}
          log={log}
          active={isActiveState(run.state)}
          composer={controls.message ? <RunComposer run={run} /> : null}
          terminal={
            terminal
              ? (shown) => (
                  <RunTerminalPanel run={run} open={terminal.open} active={shown} sessionCreatedAt={terminal.sessionCreatedAt} />
                )
              : null
          }
        />
        <div className="flex min-w-0 flex-col gap-4">
          <RunDetails run={run} viewer={viewer} />
          {plan ? <RunPlanSteps run={run} /> : null}
          <RunResult run={run} />
          {controls.owner ? <RunCredentials run={run} /> : null}
        </div>
      </div>
    </div>
  );
}

/**
 * One run: its state as a stepper, its live log, the owner's controls (cancel, take over, hand back, approve, rerun,
 * a message to the agent), its details and its result, and for its owner the credentials it got from the hub. A plan run also lists its plan's steps with their status, and
 * shows the decisions it waits on with the owner's answer form. A run of a plan the visitor may not read, or of another
 * project, is not found.
 */
export function RunDetail({ project, runId, initialError }: { project: string; runId: number; initialError: ApiErrorInfo | null }) {
  const t = useTranslations("runs.detail");
  const state = useHubQuery(runQuery(browserApi, project, runId), initialError);
  if (state.status === "error" && state.error.status === 404) {
    return <NotFoundState title={t("notFoundTitle", { id: runId })} description={t("notFoundDescription")} />;
  }
  return (
    <QueryView state={state} loading={<PageSkeleton />}>
      {(run) => <RunPage run={run} />}
    </QueryView>
  );
}
