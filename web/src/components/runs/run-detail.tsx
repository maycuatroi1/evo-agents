"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronLeft, PanelRight } from "lucide-react";
import type { Route } from "next";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useTranslations } from "next-intl";
import { type ReactNode, useCallback, useEffect, useId, useMemo, useState } from "react";

import { curatorHref } from "@/components/curator/queries";
import { useLiveSignal } from "@/components/live/live-context";
import { localAccount } from "@/components/local-run";
import { planHref, stepHref } from "@/components/plans/links";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { NotFoundState, PageSkeleton } from "@/components/states/states";
import { Button } from "@/components/ui/button";
import { workerQuery } from "@/components/workers/queries";
import { useMediaQuery, XL_QUERY } from "@/hooks/use-media-query";
import { useIsMobile } from "@/hooks/use-mobile";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";
import { chatHref } from "@/lib/chat-keys";
import { whoamiQuery } from "@/lib/queries";
import { cn } from "@/lib/utils";

import { AgentTrace, type TraceContext } from "./agent-trace";
import { useRunViewer } from "./hooks";
import { useRepoWeb } from "./links";
import { CHAT_TAB, chatKey, decisionAnchor, isActiveState, LIVE_REFRESH_MS, openDecisionsQuery, type Run, RUN_VIEW_PARAM, runKey, runQuery } from "./queries";
import { RunActions } from "./run-actions";
import { RunNotes } from "./run-notes";
import { RunChatPanel } from "./run-chat";
import { RunComposer } from "./run-composer";
import { LocalRunDetails } from "./local-run-details";
import { RunDecisions } from "./run-decisions";
import { RunHeader } from "./run-header";
import { RunLogCard, type SessionTab } from "./run-log";
import { runControls } from "./run-model";
import { useDescribeMove, useSessionTab, useTraceScreen } from "./run-page-state";
import { RunSide } from "./run-side";
import { RunTerminalPanel } from "./run-terminal";
import { RunTimeline } from "./run-timeline";
import { TracePreview } from "./trace-preview";
import { terminalAccess } from "./terminal-model";
import { logSignal, useRunLog } from "./use-run-log";

/** The plan and step under the head: `brand` links with a quiet underline, told apart from the line's words without
 * colour (the kit draws them bare; axe's link-in-text-block asks for more than a colour). */
const SUB_LINK = "rounded-xs text-brand underline decoration-brand/40 underline-offset-4 hover:decoration-current";

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
  const tPreview = useTranslations("runs.detail.preview");
  const queryClient = useQueryClient();
  const viewer = useRunViewer(run.project);
  const controls = runControls(run, viewer);
  const terminal = useRunTerminalTab(run, viewer);
  const authoring = run.kind === "author";
  const [sessionTab, setSessionTab] = useSessionTab(terminal !== null, authoring ? "chat" : "trace");

  const logId = useId();
  // A phone reads the run's head, the decision, a preview of the trace and the details, one under the other; the trace
  // opens on a screen of its own (`screen`), an entry of the browser's history, so Back closes it.
  const mobile = useIsMobile();
  const firstTab: SessionTab = authoring ? "chat" : "trace";
  const params = useSearchParams();
  const [screenOpen, openScreen, closeScreen] = useTraceScreen(setSessionTab, firstTab);
  /** Shows a tab of the session card, scrolls the card into view and focuses the tab; on a phone, opens its screen. */
  const showSession = (tab: SessionTab) => {
    if (mobile) {
      openScreen(tab);
      return;
    }
    setSessionTab(tab);
    const card = document.getElementById(logId);
    card?.scrollIntoView({ block: "start", behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
    card?.querySelector<HTMLElement>(`[data-testid="run-tab-${tab}"]`)?.focus({ preventScroll: true });
  };
  const takeOver = terminal?.open ? () => showSession("terminal") : undefined;
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
  // An author run's chat is in its log: an event the stream brings may be a message of it, so read the chat again.
  const events = log.events.length;
  useEffect(() => {
    if (authoring && events > 0) void queryClient.invalidateQueries({ queryKey: chatKey(run.project, run.id) });
  }, [authoring, events, queryClient, run.project, run.id]);

  const plan = run.kind === "plan";
  const local = run.kind === "local";
  const active = isActiveState(run.state);
  // The same query as the decisions' column (one request): "Asked you" links to the card of a decision still open.
  const decisions = useQuery({ ...openDecisionsQuery(browserApi, run.project, run.id), enabled: (plan || authoring) && active, refetchInterval: LIVE_REFRESH_MS });
  const openIds = useMemo(() => (decisions.data?.decisions ?? []).map((decision) => decision.id).join(","), [decisions.data]);
  // A step report's branch and commits link to the run's repo on that branch: its own repo, or the plan run's repo of it.
  const webOf = useRepoWeb(run.project);
  const { repo, repos } = run;
  const webOfBranch = useCallback(
    (branch: string | null) => {
      const named = (repos ?? []).find((entry) => branch !== null && entry.branch === branch)?.repo;
      return webOf(named ?? repo ?? (repos?.length === 1 ? repos[0].repo : null));
    },
    [webOf, repo, repos],
  );
  // An author run's decisions are cards in its Chat: "Asked you" shows the tab, then the card, once it is drawn.
  const showDecision = useCallback(
    (id: number) => {
      setSessionTab("chat");
      requestAnimationFrame(() =>
        requestAnimationFrame(() => {
          const card = document.getElementById(decisionAnchor(id));
          card?.scrollIntoView({ block: "center", behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
          card?.focus({ preventScroll: true });
        }),
      );
    },
    [setSessionTab],
  );
  const traceContext = useMemo<TraceContext>(
    () => ({
      runId: run.id,
      owner: run.dispatched_by,
      viewer: viewer?.login ?? null,
      describe,
      openDecisions: new Set(openIds ? openIds.split(",").map(Number) : []),
      working: run.state === "running" && log.status !== "ended",
      active,
      webOfBranch,
      showDecision: authoring ? showDecision : undefined,
    }),
    [run.id, run.dispatched_by, viewer?.login, describe, openIds, run.state, log.status, active, webOfBranch, authoring, showDecision],
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

  const sub =
          local
            ? t.rich("local.sub", { machine: run.machine ?? "", account: localAccount(run), plan: run.plan_id || "none", step: run.step_key || "none", planLink })
            : run.kind === "chat"
            ? t.rich("chatSub", {
                agent: run.profile ?? run.project,
                hop: run.hop ?? 1,
                chatLink: (chunks) =>
                  run.chat_id ? (
                    <Link href={chatHref(run.chat_id, run.chat_message_id)} className={SUB_LINK} data-testid="run-chat-link">
                      {chunks}
                    </Link>
                  ) : (
                    chunks
                  ),
              })
            : run.kind === "task"
            ? t("taskSub", { agent: run.profile ?? run.project, id: run.id })
            : authoring
            ? run.plan_id
              ? t.rich("authorPlanSub", { plan: run.plan_id, title, planLink })
              : t("authorNewSub", { title })
            : review
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
              });
  const decisionCard = plan || local ? <RunDecisions run={run} owner={controls.owner} onTakeOver={takeOver} /> : null;
  const stepLink = (key: string) => (
    <Link href={stepHref(run.project, run.plan_id, key)} className="font-mono text-xs text-brand underline-offset-4 hover:underline">
      {key}
    </Link>
  );
  const localDetails = local ? (
    <LocalRunDetails
      run={run}
      plan={run.plan_id ? <Link href={planHref(run.project, run.plan_id)} className="font-mono text-xs text-brand underline-offset-4 hover:underline">{run.plan_id}</Link> : null}
      step={run.plan_id && run.step_key ? stepLink(run.step_key) : null}
    />
  ) : null;
  const side = <RunSide run={run} viewer={viewer} owner={controls.owner} events={log.events} flat={mobile} details={localDetails} />;
  // The plan's steps and the run's details are there when asked for: the Details toggle at the end of the head opens
  // them beside the trace (under it below xl).
  const [details, setDetails] = useState(false);
  const detailsId = useId();
  const detailsToggle = (
    <Button
      type="button"
      variant={details ? "secondary" : "ghost"}
      size="sm"
      aria-expanded={details}
      aria-controls={detailsId}
      onClick={() => setDetails((open) => !open)}
      className="max-md:h-11 max-md:min-w-11"
      data-testid="run-details-open"
    >
      <PanelRight aria-hidden="true" />
      <span className="max-md:sr-only">{t("detailsToggle")}</span>
    </Button>
  );
  // On a phone the session is a screen of its own: opened from the preview, on a tab other than the first, or asked for
  // by the address (`?view=`, an author run's `?tab=chat`) until the person leaves it.
  const [asked, setAsked] = useState(() => params.has(RUN_VIEW_PARAM) || params.get("tab") === CHAT_TAB);
  const screen = mobile && (screenOpen || sessionTab !== firstTab || asked);
  const leaveScreen = () => {
    setAsked(false);
    closeScreen();
  };
  const fill = screen ? "always" : true;
  const session = (
    <div
      id={logId}
      className={cn("flex min-w-0 scroll-mt-16 flex-col xl:col-start-1 xl:row-start-1 xl:min-h-0 xl:scroll-mt-19", fill === "always" && "min-h-0 flex-1")}
    >
      <RunLogCard
        fill={fill}
        runId={run.id}
        log={log}
        frozen={{ at: frozenAt, set: setFrozenAt }}
        active={active}
        composer={controls.message && !authoring ? <RunComposer run={run} /> : null}
        chat={authoring ? (shown) => <RunChatPanel run={run} owner={controls.owner} shown={shown} fill /> : null}
        tab={sessionTab}
        onTabChange={setSessionTab}
        trace={(shown) => <AgentTrace events={log.events} status={log.status} context={traceContext} shown={shown} fill={fill} />}
        terminal={
          terminal
            ? (shown) => <RunTerminalPanel run={run} open={terminal.open} active={shown} sessionCreatedAt={terminal.sessionCreatedAt} fill />
            : null
        }
      />
    </div>
  );

  // The phases show only while the run waits for a worker, or when it ended before running: the status line says the rest.
  const showTimeline = run.state === "queued" || run.state === "leased" || (!active && !run.started_at);

  const overview = mobile && !screen;
  return (
    // From xl the page is one window tall (the window less the top bar and the gutters), and the split below the
    // timeline takes what the head leaves: as tall as the window allows, at least 30rem, the page scrolling past the
    // head only when a short window leaves less (min-h-fit, with the split's size contained so its content does not
    // count). A phone's trace screen is as tall as the window. Every mode keeps each part in its place in the tree, so
    // the session (and a connected terminal) stays mounted when the width crosses 768 px.
    <div
      className={screen ? "flex h-[calc(100dvh-5.25rem)] min-h-[28rem] flex-col gap-3" : "flex flex-col gap-6 xl:h-[calc(100dvh-6.25rem)] xl:min-h-fit"}
      data-testid="run-detail"
      data-run-id={run.id}
      data-state={run.state}
      data-kind={run.kind}
      data-screen={screen ? "session" : undefined}
    >
      {screen ? (
        <div className="-ml-2 flex items-center gap-2">
          <Button type="button" variant="ghost" size="icon" onClick={leaveScreen} aria-label={tPreview("back", { id: run.id })} data-testid="run-screen-back">
            <ChevronLeft aria-hidden="true" />
          </Button>
          <h1 className="text-[15px] leading-[22px] font-semibold">
            {t("title")} <span className="tabular-nums">#{run.id}</span>
          </h1>
        </div>
      ) : (
        <RunActions run={run} controls={controls}>
          {({ buttons, menu }) => (
            <RunHeader
              run={run}
              sub={sub}
              planHref={planHref(run.project, run.plan_id)}
              stepHref={(key) => stepHref(run.project, run.plan_id, key)}
              details={detailsToggle}
              buttons={buttons}
              menu={menu}
            />
          )}
        </RunActions>
      )}
      {screen || local ? null : <RunNotes run={run} controls={controls} />}
      {!screen && !local && showTimeline ? <RunTimeline run={run} moves={log.moves} bare /> : null}
      {/* Under 768 px: the decision, the trace's latest rows, then the details as flat sections, in the page's order. */}
      {overview ? decisionCard : null}
      {overview ? <TracePreview events={log.events} active={active} loading={log.status === "connecting"} label={authoring ? tPreview("openChat") : undefined} onOpen={() => openScreen(firstTab)} /> : null}
      {overview && details ? <div id={detailsId}>{side}</div> : null}
      {overview ? null : (
        /* From xl, two regions that scroll on their own, side by side and as tall as each other once Details is open:
           the session (an author run's Chat, Trace, Raw log, Terminal) filling the window down to its bottom gutter,
           and the side column (the decision a plan run waits on, the plan's steps, Details, Usage, the Result). From
           768 px to xl the side column comes first, its cards two by two. On a phone's trace screen, the session alone. */
        <div
          className={cn(
            screen ? "flex min-h-0 flex-1 flex-col" : "grid gap-4 xl:min-h-[30rem] xl:flex-1 xl:grid-rows-[minmax(0,1fr)] xl:[contain:size]",
            !screen && (details ? "xl:grid-cols-[minmax(0,1fr)_23rem]" : "xl:grid-cols-1"),
          )}
          data-testid={screen ? undefined : "run-split"}
        >
          {/* Closed, the trace has the page; a decision the run waits on shows above it, as it needs the owner. */}
          {screen || details ? null : decisionCard}
          {/* The side column's 4 px of padding, taken back by its margin, keep the cards' focus outlines inside its
              scroll region. Relative, so what its cards place absolutely (the words for screen readers) is clipped. */}
          {!screen && details ? (
            <section
              id={detailsId}
              aria-label={t("aside", { id: run.id })}
              tabIndex={split ? 0 : undefined}
              className="relative flex min-w-0 flex-col gap-4 xl:col-start-2 xl:row-start-1 xl:-m-1 xl:overflow-y-auto xl:overscroll-contain xl:p-1 xl:focus-visible:outline-offset-[-2px]"
              data-testid="run-aside"
            >
              {decisionCard}
              {side}
            </section>
          ) : null}
          {session}
        </div>
      )}
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
