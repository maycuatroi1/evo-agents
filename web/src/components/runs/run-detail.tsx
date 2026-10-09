"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import type { Route } from "next";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useTranslations } from "next-intl";
import { type ReactNode, useCallback, useEffect, useId, useMemo, useState } from "react";

import { ReviewRunKindBadge } from "@/components/curator/badges";
import { curatorHref } from "@/components/curator/queries";
import { useLiveSignal } from "@/components/live/live-context";
import { planHref, stepHref } from "@/components/plans/links";
import { PageHeader } from "@/components/shell/page-header";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { NotFoundState, PageSkeleton } from "@/components/states/states";
import { StatusBadge, useStatusText } from "@/components/status/status-badge";
import { workerQuery } from "@/components/workers/queries";
import { useMediaQuery, XL_QUERY } from "@/hooks/use-media-query";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";
import { whoamiQuery } from "@/lib/queries";

import { AgentTrace, type TraceContext } from "./agent-trace";
import { PlanRunKindBadge } from "./badges";
import { useRunViewer } from "./hooks";
import type { RunMove } from "./log-model";
import { isActiveState, LIVE_REFRESH_MS, openDecisionsQuery, type Run, RUN_VIEW_PARAM, runKey, runQuery } from "./queries";
import { RunActions, RunNotes } from "./run-actions";
import { RunComposer } from "./run-composer";
import { RunCredentials } from "./run-credentials";
import { RunDecisions } from "./run-decisions";
import { RunDetails, RunResult } from "./run-facts";
import { RunLogCard, SESSION_TABS, type SessionTab } from "./run-log";
import { runControls } from "./run-model";
import { RunPlanSteps } from "./run-plan-steps";
import { RunTerminalPanel } from "./run-terminal";
import { RunTimeline } from "./run-timeline";
import { terminalAccess } from "./terminal-model";
import { UsageMeter } from "./usage-meter";
import { logSignal, useRunLog } from "./use-run-log";

/** The plan and step under the head: `brand` links with a quiet underline, told apart from the line's words without
 * colour (the kit draws them bare; axe's link-in-text-block asks for more than a colour). */
const SUB_LINK = "rounded-xs text-brand underline decoration-brand/40 underline-offset-4 hover:decoration-current";

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
 * The tab the session card shows, held by the page and kept in the URL (`?view=log`, `?view=terminal`; the Trace, the
 * first tab, has none): the Terminal tab once offered when the page was opened on it (a decision's Take over from the
 * Inbox), or when a decision's Take over here asks for it. Changing tabs replaces the URL without a navigation.
 */
function useSessionTab(terminalOffered: boolean) {
  const params = useSearchParams();
  const asked = params.get(RUN_VIEW_PARAM);
  const wanted: SessionTab | null = (SESSION_TABS as readonly string[]).includes(asked ?? "") ? (asked as SessionTab) : null;
  const [tab, setTab] = useState<SessionTab>(wanted === "log" ? "log" : "trace");
  const [applied, setApplied] = useState(false);
  if (wanted === "terminal" && terminalOffered && !applied) {
    setApplied(true);
    setTab("terminal");
  }
  const choose = useCallback((next: SessionTab) => {
    setTab(next);
    const url = new URL(window.location.href);
    if (next === "trace") url.searchParams.delete(RUN_VIEW_PARAM);
    else url.searchParams.set(RUN_VIEW_PARAM, next);
    window.history.replaceState(window.history.state, "", `${url.pathname}${url.search}${url.hash}`);
  }, []);
  return [tab, choose] as const;
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

  const plan = run.kind === "plan";
  const active = isActiveState(run.state);
  // The same query as the decisions' column (one request): "Asked you" links to the card of a decision still open.
  const decisions = useQuery({ ...openDecisionsQuery(browserApi, run.project, run.id), enabled: plan && active, refetchInterval: LIVE_REFRESH_MS });
  const openIds = useMemo(() => (decisions.data?.decisions ?? []).map((decision) => decision.id).join(","), [decisions.data]);
  const traceContext = useMemo<TraceContext>(
    () => ({
      runId: run.id,
      owner: run.dispatched_by,
      viewer: viewer?.login ?? null,
      describe,
      openDecisions: new Set(openIds ? openIds.split(",").map(Number) : []),
      working: run.state === "running" && log.status !== "ended",
      active,
    }),
    [run.id, run.dispatched_by, viewer?.login, describe, openIds, run.state, log.status, active],
  );

  // From xl the side column is a scroll region of its own, which a key must reach; below, the page scrolls it.
  const split = useMediaQuery(XL_QUERY);

  const review = run.kind === "review";
  const title = run.title ?? (plan ? run.plan_id : t("untitled"));
  const planLink = (chunks: ReactNode) => (
    <Link href={planHref(run.project, run.plan_id)} className={SUB_LINK} data-testid="run-plan-link">
      {chunks}
    </Link>
  );

  return (
    // From xl the page is one window tall (the window less the top bar and the gutters), and the split below the
    // timeline takes what the head leaves: as tall as the window allows, at least 30rem, the page scrolling past the
    // head only when a short window leaves less (min-h-fit, with the split's size contained so its content does not
    // count).
    <div
      className="flex flex-col gap-6 xl:h-[calc(100dvh-6.25rem)] xl:min-h-fit"
      data-testid="run-detail"
      data-run-id={run.id}
      data-state={run.state}
      data-kind={run.kind}
    >
      <PageHeader
        // "Run #12" for every run, as the kit's RunScreen: a plan run says what it is once, in its Plan run tag.
        title={
          <>
            {t("title")} <span className="tabular-nums">#{run.id}</span>
          </>
        }
        status={<StatusBadge kind="run" status={run.state} size="lg" />}
        tags={plan ? <PlanRunKindBadge /> : review ? <ReviewRunKindBadge /> : null}
        sub={
          review
            ? t.rich("reviewSub", {
                curatorLink: (chunks) => (
                  <Link href={curatorHref(run.project)} className={SUB_LINK} data-testid="run-curator-link">
                    {chunks}
                  </Link>
                ),
                proposalsLink: (chunks) => (
                  <Link href={`${curatorHref(run.project, "proposals")}?run=${run.id}` as Route} className={SUB_LINK} data-testid="run-proposals-link">
                    {chunks}
                  </Link>
                ),
              })
            : plan || run.step_key === null
            ? // The plan once, as its link; its title after it only when it says more than the id.
              run.title && run.title !== run.plan_id
              ? t.rich("planSubTitled", { plan: run.plan_id, title: run.title, planLink })
              : t.rich("planSub", { plan: run.plan_id, planLink })
            : t.rich("sub", {
                plan: run.plan_id,
                step: run.step_key,
                title,
                planLink,
                stepLink: (chunks) => (
                  <Link
                    href={stepHref(run.project, run.plan_id, run.step_key ?? "")}
                    className={SUB_LINK}
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
      <RunTimeline run={run} moves={log.moves} />
      {/* The kit's run screen. From xl, two regions that scroll on their own, side by side and as tall as each other:
          the session (Trace, Raw log, Terminal) on the left, filling the window down to its bottom gutter, and the side
          column (the decision a plan run waits on, Details, Usage, the plan's steps, the Result, Credentials) on the
          right. Below xl the side column moves under the timeline, the decision first, then the session: the page
          reads in that order at every width, and from md to xl the side cards sit two by two. */}
      <div
        className="grid gap-4 xl:min-h-[30rem] xl:flex-1 xl:grid-cols-[minmax(0,1fr)_23rem] xl:grid-rows-[minmax(0,1fr)] xl:[contain:size]"
        data-testid="run-split"
      >
        {/* The side column's 4 px of padding, taken back by its margin, keep the cards' shadows and focus outlines
            inside its scroll region, so its edges and the session card's line up. Relative, so what its cards place
            absolutely (the words for screen readers) is clipped with them, instead of lengthening the page. */}
        <section
          aria-label={t("aside", { id: run.id })}
          tabIndex={split ? 0 : undefined}
          className="relative flex min-w-0 flex-col gap-4 xl:col-start-2 xl:row-start-1 xl:-m-1 xl:overflow-y-auto xl:overscroll-contain xl:p-1 xl:focus-visible:outline-offset-[-2px]"
          data-testid="run-aside"
        >
          {plan ? <RunDecisions run={run} owner={controls.owner} onTakeOver={takeOver} /> : null}
          <div className="grid min-w-0 items-start gap-4 md:grid-cols-2 xl:grid-cols-1" data-testid="run-side">
            <RunDetails run={run} viewer={viewer} />
            <UsageMeter run={run} events={log.events} />
            {plan ? <RunPlanSteps run={run} /> : null}
            <RunResult run={run} />
            {controls.owner ? <RunCredentials run={run} /> : null}
          </div>
        </section>
        <div id={logId} className="flex min-w-0 scroll-mt-16 flex-col xl:col-start-1 xl:row-start-1 xl:min-h-0 xl:scroll-mt-19">
          <RunLogCard
            fill
            runId={run.id}
            log={log}
            frozen={{ at: frozenAt, set: setFrozenAt }}
            active={active}
            composer={controls.message ? <RunComposer run={run} /> : null}
            tab={sessionTab}
            onTabChange={setSessionTab}
            trace={(shown) => <AgentTrace events={log.events} status={log.status} context={traceContext} shown={shown} fill />}
            terminal={
              terminal
                ? (shown) => (
                    <RunTerminalPanel run={run} open={terminal.open} active={shown} sessionCreatedAt={terminal.sessionCreatedAt} fill />
                  )
                : null
            }
          />
        </div>
      </div>
    </div>
  );
}

/**
 * One run: its phases as a timeline, its trace and raw log (live), the owner's controls (cancel, take over, hand back,
 * approve, rerun, a message to the agent), its details, its usage and its result, and for its owner the credentials it
 * got from the hub. A plan run also lists its plan's steps with their status, and shows the decisions it waits on with
 * the owner's answer form at the top of the side column. A run of a plan the visitor may not read, or of another
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
