"use client";

import { useQuery, useQueryClient, type UseQueryResult } from "@tanstack/react-query";
import { MessageSquare, RotateCw, TriangleAlert, X } from "lucide-react";
import { useTranslations } from "next-intl";
import { type ReactNode, useEffect, useState } from "react";

import { RunRef } from "@/components/data/identifier";
import type { OverviewDecision, OverviewRun } from "@/components/home/model";
import { PlanRunKindBadge } from "@/components/runs/badges";
import type { DescribeMove } from "@/components/runs/log-model";
import { isActiveState, LIVE_REFRESH_MS, type Run, runHref, runKey } from "@/components/runs/queries";
import { type LogStatus, logSignal, useRunLog } from "@/components/runs/use-run-log";
import { StatusBadge, useStatusText } from "@/components/status/status-badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { browserApi } from "@/lib/api/browser";
import type { ApiError } from "@/lib/api/errors";
import { planQuery } from "@/lib/plan-queries";
import { countSteps, parsePlan } from "@/lib/plans";
import { cn } from "@/lib/utils";

import { KEEP_EVENTS, tailStart, type TileRef, tileKey } from "./model";
import { RunTime, StepsDone, WorkerName } from "./parts";
import { type TileSignals, useTileSignal } from "./signals";
import { TileTrace } from "./tile-trace";

/**
 * One tile of the Monitor's grid, for viewing only: the run's number (a link to its page), its project, its title, its
 * state as a pill (the pulsing dot only while it runs), its worker, its time and a plan run's steps, "Waiting for you"
 * while it waits on the visitor's answer, and the tail of its trace, live. A close button takes it off the grid. No
 * Cancel and no message box: those are on the run's page and in the Inbox.
 *
 * The run is read by the page (every 5 seconds while it is active) and handed in; the tile follows its trace from
 * TAIL_EVENTS events before the run's last_seq with an EventSource (`useRunLog`: reads of events while the stream
 * fails, the stream closed once the run ended, when the tile closes or when the page is left). A run that ended keeps
 * its tile in its last state until the visitor closes it.
 */

/** The tile's log describes no move: moves show as the pill, not as lines. */
const NO_MOVES: DescribeMove = () => "";

type TileProps = {
  tile: TileRef;
  query: UseQueryResult<Run, ApiError>;
  /** The run as the overview has it (in flight or ended lately), for a plan run's title and steps. */
  flight: OverviewRun | null;
  /** The open decision the run waits on, when the overview lists one. */
  decision: OverviewDecision | null;
  viewer: string | null;
  signals: TileSignals;
  onClose: () => void;
};

/** The tile: its head (the number, the project, the pill and Close, then `children`), then `body` filling the rest. */
function TileFrame({
  tile,
  state,
  pill,
  children,
  body,
  onClose,
}: {
  tile: TileRef;
  state: string;
  pill?: ReactNode;
  children?: ReactNode;
  body: ReactNode;
  onClose: () => void;
}) {
  const t = useTranslations("monitor.tile");
  return (
    <article
      aria-label={t("label", { id: tile.id })}
      className="flex h-full min-h-0 min-w-0 flex-col overflow-hidden rounded-md border bg-card shadow-raised"
      data-testid="monitor-tile"
      data-run-id={tile.id}
      data-project={tile.project}
      data-state={state}
    >
      <header className="flex min-w-0 flex-col gap-1 border-b px-3 py-2">
        <div className="flex min-w-0 items-center gap-2">
          <h3 className="flex min-w-0 items-center gap-2 text-sm leading-5">
            <RunRef
              id={tile.id}
              href={runHref(tile.project, tile.id)}
              label={t("open", { id: tile.id })}
              className="max-md:inline-flex max-md:min-h-11 max-md:items-center"
              data-testid="tile-run-link"
            />
            <span className="truncate text-xs text-fg-subtle" data-testid="tile-project">
              {tile.project}
            </span>
          </h3>
          <div className="ml-auto flex shrink-0 items-center gap-1">
            {pill}
            <Button type="button" variant="ghost" size="icon-sm" aria-label={t("close", { id: tile.id })} onClick={onClose} data-testid="tile-close">
              <X aria-hidden="true" />
            </Button>
          </div>
        </div>
        {children}
      </header>
      {body}
    </article>
  );
}

/** The stream's state at the end of the tile's facts: a still dot in its tone and a word; nothing once it ended. */
const STREAM_DOT: Record<LogStatus, string> = {
  connecting: "bg-neutral-solid",
  live: "bg-success-solid",
  reconnecting: "bg-attention-solid",
  polling: "bg-attention-solid",
  ended: "bg-neutral-solid",
  failed: "bg-danger-solid",
};

function StreamState({ status }: { status: LogStatus }) {
  const t = useTranslations("runs.detail.log.status");
  if (status === "ended") return null;
  return (
    <span className="ml-auto inline-flex items-center gap-1.5 whitespace-nowrap" data-testid="tile-stream" data-status={status}>
      <span className={cn("size-1.5 shrink-0 rounded-full", STREAM_DOT[status])} aria-hidden="true" />
      {t(status)}
    </span>
  );
}

/** A plan run's steps done of the total: from the overview while it has the run, else from the plan. */
function usePlanSteps(run: Run, flight: OverviewRun | null): { done: number; total: number } | null {
  const fromOverview = flight !== null && flight.steps_total !== null && flight.steps_done !== null;
  const needPlan = run.kind === "plan" && !fromOverview;
  const plan = useQuery({
    ...planQuery(browserApi, run.project, run.plan_id),
    enabled: needPlan,
    refetchInterval: needPlan && isActiveState(run.state) ? LIVE_REFRESH_MS : false,
  });
  if (run.kind !== "plan") return null;
  if (fromOverview) return { done: flight.steps_done ?? 0, total: flight.steps_total ?? 0 };
  if (!plan.data) return null;
  const counts = countSteps(parsePlan(plan.data.body, plan.data.plan_id).steps);
  return { done: counts.done, total: counts.total };
}

function LiveTile({ tile, run, flight, decision, viewer, signals, onClose }: Omit<TileProps, "query"> & { run: Run }) {
  const t = useTranslations("monitor.tile");
  const tRuns = useTranslations("runs");
  const tState = useStatusText("run");
  const queryClient = useQueryClient();
  // Where the tail starts is read once, when the tile opens: a later last_seq moves nothing.
  const [start] = useState(() => tailStart(run.last_seq));
  const log = useRunLog({ project: tile.project, runId: tile.id, knownLastSeq: run.last_seq, describe: NO_MOVES, startAfter: start, keep: KEEP_EVENTS });
  useTileSignal(signals, tileKey(tile), logSignal(log, false));

  // A move the trace tells, or the end of the stream, changes the run: read it again at once rather than at its next tick.
  const lastMove = log.moves.at(-1)?.seq ?? 0;
  const ended = log.status === "ended";
  useEffect(() => {
    if (lastMove > 0 || ended) void queryClient.invalidateQueries({ queryKey: runKey(tile.project, tile.id), exact: true });
  }, [lastMove, ended, queryClient, tile.project, tile.id]);

  // Said once to screen readers when the run moves: the tile's own region of state, quiet until then.
  const [seen, setSeen] = useState(run.state);
  const [said, setSaid] = useState("");
  if (seen !== run.state) {
    setSeen(run.state);
    setSaid(t("now", { id: tile.id, state: tState(run.state) }));
  }

  const plan = run.kind === "plan" || run.step_key === null;
  // A review run of the Curator works on no plan: its title may be all there is.
  const title = (plan ? (flight?.plan_title ?? run.title ?? run.plan_id) : (run.title ?? run.step_key ?? run.plan_id)) || tRuns("untitled");
  const steps = usePlanSteps(run, flight);
  const active = isActiveState(run.state);
  const waiting = decision !== null && (run.state === "waiting" || run.state === "parked");
  const partial = (log.events[0]?.seq ?? start + 1) > 1;

  return (
    <TileFrame
      tile={tile}
      state={run.state}
      onClose={onClose}
      pill={<StatusBadge kind="run" status={run.state} />}
      body={
        <TileTrace
          runId={tile.id}
          events={log.events}
          status={log.status}
          owner={run.dispatched_by}
          viewer={viewer}
          active={active}
          partial={partial}
          error={run.state === "failed" || run.state === "lost" ? run.error : null}
        />
      }
    >
      <p className="flex min-w-0 items-center gap-2 text-[13px] leading-[18px] font-medium">
        <span className="truncate" title={title} data-testid="tile-title">
          {title}
        </span>
        {run.kind === "plan" ? <PlanRunKindBadge /> : null}
      </p>
      <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1 text-xs text-fg-subtle" data-testid="tile-facts">
        <WorkerName worker={run.worker} />
        <RunTime run={run} />
        {steps ? <StepsDone done={steps.done} total={steps.total} testId="tile-steps" /> : null}
        {waiting && decision.yours ? (
          <span className="inline-flex items-center gap-1 font-medium text-attention" data-testid="tile-waiting-you">
            <MessageSquare className="size-3.5" aria-hidden="true" />
            {t("waitingYou")}
          </span>
        ) : waiting ? (
          <span data-testid="tile-waiting-for">{t("waitingFor", { owner: decision.owner })}</span>
        ) : null}
        <StreamState status={log.status} />
      </div>
      <span role="status" className="sr-only" data-testid="tile-said">
        {said}
      </span>
    </TileFrame>
  );
}

export function RunTile(props: TileProps) {
  const t = useTranslations("monitor.tile");
  const { tile, query, onClose } = props;
  if (query.data) return <LiveTile {...props} run={query.data} />;
  if (query.isError) {
    return (
      <TileFrame
        tile={tile}
        state="error"
        onClose={onClose}
        body={
          <div className="flex flex-1 flex-col items-start gap-2 px-3 py-3 text-[13px] text-danger" data-testid="tile-error">
            <span className="inline-flex items-center gap-1.5">
              <TriangleAlert className="size-4 shrink-0" aria-hidden="true" />
              {t("error", { id: tile.id })}
            </span>
            <Button type="button" variant="secondary" size="sm" onClick={() => void query.refetch()} busy={query.isFetching}>
              <RotateCw aria-hidden="true" />
              {query.isFetching ? t("retrying") : t("retry")}
            </Button>
          </div>
        }
      />
    );
  }
  return (
    <TileFrame
      tile={tile}
      state="loading"
      onClose={onClose}
      body={<div className="min-h-0 flex-1 bg-term-bg" aria-hidden="true" />}
    >
      <div className="flex flex-col gap-2 py-1" aria-hidden="true" data-testid="tile-loading">
        <Skeleton className="h-3.5 w-3/5 rounded-xs" />
        <Skeleton className="h-3 w-2/5 rounded-xs" />
      </div>
    </TileFrame>
  );
}
