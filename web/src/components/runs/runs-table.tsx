"use client";

import Link from "next/link";
import { useFormatter, useTranslations } from "next-intl";
import { createContext, type ReactNode, useCallback, useContext, useId, useMemo, useState } from "react";

import type { DataListRow } from "@/components/data/data-list";
import { CellMain, DataTable, dataTableColumns } from "@/components/data/data-table";
import { Identifier, NAME_LINK, RunRef } from "@/components/data/identifier";
import { useNow } from "@/components/kg/use-now";
import { planHref, stepHref } from "@/components/plans/links";
import { StatusBadge } from "@/components/status/status-badge";
import { workerHref } from "@/components/workers/queries";
import { useRuntimeName } from "@/lib/runtimes";
import { cn } from "@/lib/utils";

import { AuthorRunKindBadge, ChatRunKindBadge, PlanRunKindBadge, TaskRunKindBadge } from "./badges";
import { hasOtherTurns, isSessionTurn } from "./chat-session";
import { SessionTurns, TurnsToggle } from "./chat-session-turns";
import { LocalRunKindBadge, localAccount } from "@/components/local-run";
import { readRunUsage } from "./usage-model";
import { durationParts, runTiming, taskBranch } from "./model";
import { isActiveState, type Run, runHref } from "./queries";

/** Who reads the table: a worker links to its page only for the member who owns it (or a hub admin). */
export type Viewer = { login: string; admin: boolean };

/** The columns differ by page: the project's runs, one step's runs (no step column), one worker's runs. */
export type RunsTableVariant = "project" | "step" | "worker";

/** Columns that hide on narrow screens, per variant: the step page's table sits beside the step navigator. */
const NARROW_HIDDEN: Record<RunsTableVariant, Record<string, string>> = {
  project: {
    worker: "hidden sm:table-cell",
    runtime: "hidden lg:table-cell",
    started: "hidden md:table-cell",
    duration: "hidden sm:table-cell",
    attempt: "hidden xl:table-cell",
  },
  step: {
    dispatcher: "hidden md:table-cell",
    runtime: "hidden 2xl:table-cell",
    started: "hidden md:table-cell",
    duration: "hidden sm:table-cell",
    attempt: "hidden 2xl:table-cell",
  },
  worker: {
    runtime: "hidden lg:table-cell",
    started: "hidden md:table-cell",
    duration: "hidden sm:table-cell",
    attempt: "hidden xl:table-cell",
  },
};

/** Which columns each variant shows, in order. */
const COLUMNS: Record<RunsTableVariant, string[]> = {
  project: ["run", "step", "state", "cost", "worker", "runtime", "started", "duration", "attempt"],
  step: ["run", "state", "cost", "worker", "dispatcher", "runtime", "started", "duration", "attempt"],
  worker: ["run", "step", "state", "cost", "runtime", "started", "duration", "attempt"],
};

/** What the run cost in dollars, as its runtime reported it; a dash when it reported none. */
function CostCell({ run }: { run: Run }) {
  const format = useFormatter();
  const cost = readRunUsage(run.usage)?.cost;
  if (!cost) return <span className="text-fg-subtle">-</span>;
  const amount = format.number(cost.amount, {
    style: "currency",
    currency: cost.currency,
    maximumFractionDigits: cost.amount > 0 && cost.amount < 0.01 ? 4 : 2,
  });
  return (
    <span className="whitespace-nowrap font-mono text-xs" data-testid="run-cost">
      {amount}
    </span>
  );
}

/** A plan run's progress: how much of its plan is done and the step it is at, with that step's status. */
function PlanProgressLine({ run }: { run: Run }) {
  const t = useTranslations("runs.progress");
  const progress = run.plan_progress;
  if (!progress) return null;
  return (
    <span className="max-w-56 text-xs text-fg-subtle" data-testid="run-progress">
      {progress.current_step
        ? t("at", { percent: progress.percent, done: progress.done, total: progress.total, step: progress.current_step, status: progress.current_status ?? "" })
        : t("complete", { percent: progress.percent, done: progress.done, total: progress.total })}
    </span>
  );
}

export function useDuration() {
  const t = useTranslations("runs.duration");
  return (ms: number) => {
    const { hours, minutes } = durationParts(ms);
    if (hours > 0) return t("hours", { hours, minutes });
    return minutes > 0 ? t("minutes", { minutes }) : t("lessThanMinute");
  };
}

function Timing({ run, part }: { run: Run; part: "started" | "duration" }) {
  const t = useTranslations("runs");
  const format = useFormatter();
  const duration = useDuration();
  const now = useNow(isActiveState(run.state));
  const timing = runTiming(run, now);
  if (part === "started") {
    const at = timing.startedAt ?? run.queued_at;
    const date = new Date(at);
    return (
      <time
        dateTime={at}
        title={format.dateTime(date, { dateStyle: "medium", timeStyle: "medium" })}
        className={cn("whitespace-nowrap", !timing.startedAt && "text-fg-subtle")}
      >
        {timing.startedAt ? null : <span className="sr-only">{t("queuedAt")} </span>}
        {format.dateTime(date, { dateStyle: "short", timeStyle: "short" })}
      </time>
    );
  }
  if (timing.durationMs === null) return <span className="text-fg-subtle">-</span>;
  return (
    <span className={cn("whitespace-nowrap", timing.waiting && "text-fg-subtle")} data-testid="run-duration">
      {timing.waiting ? t("waiting", { duration: duration(timing.durationMs) }) : duration(timing.durationMs)}
    </span>
  );
}

/**
 * A chat session's row of a grouped table, its latest turn: open (its turns show under it) or not, and how to toggle it.
 * Null for any other row, and for every row of a table that lists each run.
 */
type SessionRow = { open: boolean; controls: string; onToggle: () => void } | null;

/** How a cell finds its row's session, so opening one changes no column, which would draw every cell anew. */
const SessionRows = createContext<(run: Run) => SessionRow>(() => null);

/**
 * The run's title and one line under it: its plan and step (and project on a worker's page), or, once it failed, was
 * lost or cancelled, the reason in danger. A plan run carries the Plan run tag after its title. A chat session's row
 * (`SessionRows`) has the session's title, whose session it is (the person and the agent), and its turns' disclosure.
 */
function StepCell({ run, showProject }: { run: Run; showProject: boolean }) {
  const t = useTranslations("runs");
  const session = useContext(SessionRows)(run);
  const ended = run.state === "failed" || run.state === "lost" || run.state === "cancelled";
  const plan = run.kind === "plan" || run.step_key === null;
  const repos = run.repos?.length ?? 0;
  const authorNew = run.kind === "author" && !run.plan_id; // an author run of a new plan: no plan to link to yet
  const chat = run.kind === "chat"; // a turn of an agent in a group chat: no plan, the run's page is where it leads
  const task = run.kind === "task"; // a turn of a plugin's agent type on a task: no plan, its page is where it leads
  const local = run.kind === "local"; // a member's own Claude Code session: no plan, its page is where it leads
  const localLine = { project: run.project, machine: run.machine ?? "", account: localAccount(run), plan: run.plan_id || "none", step: run.step_key || "none" };
  const where = session
    ? t("session.line", { person: run.dispatched_by, agent: run.profile ?? run.project })
    : local
    ? showProject
      ? t("local.lineProject", localLine)
      : t("local.line", localLine)
    : chat
    ? showProject
      ? t("chatRun.lineProject", { project: run.project, agent: run.profile ?? run.project })
      : t("chatRun.line", { agent: run.profile ?? run.project })
    : task
    ? showProject
      ? t("taskRun.lineProject", { project: run.project, agent: run.profile ?? run.project, branch: taskBranch(run) })
      : t("taskRun.line", { agent: run.profile ?? run.project, branch: taskBranch(run) })
    : authorNew
    ? showProject
      ? t("author.newPlanLineProject", { project: run.project })
      : t("author.newPlanLine")
    : plan
    ? showProject
      ? t("planLineProject", { project: run.project, plan: run.plan_id, repos })
      : t("planLine", { plan: run.plan_id, repos })
    : showProject
      ? t("stepLineProject", { project: run.project, plan: run.plan_id, step: run.step_key ?? "" })
      : t("stepLine", { plan: run.plan_id, step: run.step_key ?? "" });
  const failure = ended && run.error ? run.error : null;
  const title = session ? (run.session_title ?? run.title ?? t("untitled")) : plan && !local ? (run.title ?? run.plan_id) : (run.title ?? t("untitled"));
  return (
    <CellMain
      sub={failure ?? where}
      subTitle={failure ? `${where}: ${failure}` : where}
      danger={failure !== null}
      subTestId={failure ? "run-error" : "run-where"}
    >
      <Link
        href={authorNew || chat || task || local ? runHref(run.project, run.id) : plan ? planHref(run.project, run.plan_id) : stepHref(run.project, run.plan_id, run.step_key ?? "")}
        className={cn(NAME_LINK, "truncate")}
        title={title}
        data-testid={plan && !local ? "run-plan-link" : "run-step-link"}
      >
        {title}
      </Link>
      {local ? <LocalRunKindBadge /> : run.kind === "author" ? <AuthorRunKindBadge /> : chat ? <ChatRunKindBadge /> : task ? <TaskRunKindBadge /> : plan ? <PlanRunKindBadge /> : null}
      {session && hasOtherTurns(run) ? <TurnsToggle run={run} open={session.open} controls={session.controls} onToggle={session.onToggle} /> : null}
    </CellMain>
  );
}

function WorkerCell({ run, viewer }: { run: Run; viewer: Viewer | null }) {
  const t = useTranslations("runs");
  if (run.kind === "local") return <Identifier value={run.machine ?? t("local.noWorker")} />;
  if (run.worker_id === null || !run.worker) {
    return (
      <span className="text-fg-subtle">{run.pinned_worker_id !== null ? t("pinnedWaiting") : t("noWorkerYet")}</span>
    );
  }
  const mayOpen = viewer !== null && (viewer.admin || viewer.login === run.dispatched_by);
  return <Identifier value={run.worker} href={mayOpen ? workerHref(run.worker_id) : undefined} />;
}

/**
 * A run as a phone lists it: its title (the step's, the plan's for a plan run, or "Run #12" on the step's own page)
 * opening the run, its state, and one line with its number and where it ran from (its worker, on the step's page), or
 * why it failed.
 */
function useMobileRun(variant: RunsTableVariant, grouped: boolean) {
  const t = useTranslations("runs");
  // A chat session as a phone lists it: its title, the latest turn's state, whose session and its turns; the row opens
  // the latest turn, whose page links every turn.
  const sessionMobile = (run: Run): DataListRow => {
    const ended = run.state === "failed" || run.state === "lost" || run.state === "cancelled";
    const failure = ended && run.error ? run.error : null;
    const detail = failure ?? t("session.mobileLine", { person: run.dispatched_by, agent: run.profile ?? run.project, count: run.session_turns ?? 1 });
    return {
      title: run.session_title ?? run.title ?? t("untitled"),
      href: runHref(run.project, run.id),
      tags: <ChatRunKindBadge />,
      status: <StatusBadge kind="run" status={run.state} />,
      meta: t.rich("mobile.meta", { id: run.id, detail, ref: (chunks: ReactNode) => <span className="font-mono tabular-nums">{chunks}</span> }),
      metaText: t("mobile.metaText", { id: run.id, detail }),
      danger: failure !== null,
      data: { "run-id": run.id, state: run.state },
    };
  };
  return (run: Run): DataListRow => {
    if (grouped && isSessionTurn(run)) return sessionMobile(run);
    const ended = run.state === "failed" || run.state === "lost" || run.state === "cancelled";
    const plan = run.kind === "plan" || run.step_key === null;
    const failure = ended && run.error ? run.error : null;
    const worker = run.worker_id !== null && run.worker ? run.worker : run.pinned_worker_id !== null ? t("pinnedWaiting") : t("noWorkerYet");
    const authorNew = run.kind === "author" && !run.plan_id;
    const chat = run.kind === "chat";
    const task = run.kind === "task";
    const local = run.kind === "local";
    const where =
      variant === "step"
        ? worker
        : local
          ? t(variant === "worker" ? "local.lineProject" : "local.line", { project: run.project, machine: run.machine ?? "", account: localAccount(run), plan: run.plan_id || "none", step: run.step_key || "none" })
        : chat
          ? t(variant === "worker" ? "chatRun.lineProject" : "chatRun.line", { project: run.project, agent: run.profile ?? run.project })
        : task
          ? t(variant === "worker" ? "taskRun.lineProject" : "taskRun.line", { project: run.project, agent: run.profile ?? run.project, branch: taskBranch(run) })
          : authorNew
          ? t(variant === "worker" ? "author.newPlanLineProject" : "author.newPlanLine", { project: run.project })
          : plan
          ? t(variant === "worker" ? "planLineProject" : "planLine", { project: run.project, plan: run.plan_id, repos: run.repos?.length ?? 0 })
          : t(variant === "worker" ? "stepLineProject" : "stepLine", { project: run.project, plan: run.plan_id, step: run.step_key ?? "" });
    const detail = failure ?? where;
    return {
      title: variant === "step" ? t("mobile.runTitle", { id: run.id }) : plan && !local ? (run.title ?? run.plan_id) : (run.title ?? t("untitled")),
      href: runHref(run.project, run.id),
      tags: local ? <LocalRunKindBadge /> : run.kind === "author" ? <AuthorRunKindBadge /> : chat ? <ChatRunKindBadge /> : task ? <TaskRunKindBadge /> : plan ? <PlanRunKindBadge /> : null,
      status: <StatusBadge kind="run" status={run.state} />,
      meta: t.rich("mobile.meta", { id: run.id, detail, ref: (chunks: ReactNode) => <span className="font-mono tabular-nums">{chunks}</span> }),
      metaText: t("mobile.metaText", { id: run.id, detail }),
      danger: failure !== null,
      data: { "run-id": run.id, state: run.state },
    };
  };
}

/**
 * Runs as a table, newest first as the API sends them; the order is the server's, so the columns do not sort. A
 * `grouped` table (the API's `group=session`) lists a chat session as its latest turn, whose row opens into its turns.
 */
export function RunsTable({
  runs,
  caption,
  viewer,
  variant = "project",
  testId = "runs-table",
  grouped = false,
}: {
  runs: Run[];
  caption: string;
  viewer: Viewer | null;
  variant?: RunsTableVariant;
  testId?: string;
  grouped?: boolean;
}) {
  const t = useTranslations("runs");
  const tRuntime = useRuntimeName();
  const mobile = useMobileRun(variant, grouped);
  // The sessions open, by their first run: a session keeps its place and stays open as new turns arrive.
  const [open, setOpen] = useState<ReadonlySet<number>>(() => new Set());
  const baseId = useId();
  const sessionOf = useCallback(
    (run: Run): SessionRow => {
      if (!grouped || !isSessionTurn(run)) return null;
      const session = run.session_run_id as number;
      const onToggle = () =>
        setOpen((shown) => {
          const next = new Set(shown);
          if (!next.delete(session)) next.add(session);
          return next;
        });
      return { open: open.has(session), controls: `${baseId}-session-${session}`, onToggle };
    },
    [grouped, open, baseId],
  );
  const columns = useMemo(() => {
    const helper = dataTableColumns<Run>();
    const run = helper.display({
      id: "run",
      header: () => t("columns.run"),
      cell: (info) => (
        <RunRef
          id={info.row.original.id}
          href={runHref(info.row.original.project, info.row.original.id)}
          label={t("openRun", { id: info.row.original.id })}
          data-run-id={info.row.original.id}
          data-testid="run-link"
        />
      ),
    });
    const step = helper.display({
      id: "step",
      header: () => t("columns.step"),
      meta: { primary: true },
      cell: (info) => <StepCell run={info.row.original} showProject={variant === "worker"} />,
    });
    const state = helper.display({
      id: "state",
      header: () => t("columns.state"),
      cell: (info) => {
        const run = info.row.original;
        return (
          <div className="flex flex-col items-start gap-1">
            <StatusBadge kind="run" status={run.state} />
            <PlanProgressLine run={run} />
          </div>
        );
      },
    });
    const cost = helper.display({
      id: "cost",
      header: () => t("columns.cost"),
      meta: { numeric: true },
      cell: (info) => <CostCell run={info.row.original} />,
    });
    const worker = helper.display({
      id: "worker",
      header: () => t("columns.worker"),
      cell: (info) => <WorkerCell run={info.row.original} viewer={viewer} />,
    });
    const dispatcher = helper.display({
      id: "dispatcher",
      header: () => t("columns.dispatcher"),
      cell: (info) => <span className="font-mono text-xs text-foreground">{info.row.original.dispatched_by}</span>,
    });
    const runtime = helper.display({
      id: "runtime",
      header: () => t("columns.runtime"),
      cell: (info) => <span className="whitespace-nowrap">{tRuntime(info.row.original.runtime)}</span>,
    });
    const started = helper.display({
      id: "started",
      header: () => t("columns.started"),
      meta: { numeric: true },
      cell: (info) => <Timing run={info.row.original} part="started" />,
    });
    const duration = helper.display({
      id: "duration",
      header: () => t("columns.duration"),
      meta: { numeric: true },
      cell: (info) => <Timing run={info.row.original} part="duration" />,
    });
    const attempt = helper.display({
      id: "attempt",
      header: () => t("columns.attempt"),
      meta: { numeric: true },
      cell: (info) => {
        const { attempt: number, max_attempts: max } = info.row.original;
        return (
          <span className="font-mono text-xs">
            <span aria-hidden="true">
              {number}/{max}
            </span>
            <span className="sr-only">{t("attemptLabel", { attempt: number, max })}</span>
          </span>
        );
      },
    });
    const all = { run, step, state, cost, worker, dispatcher, runtime, started, duration, attempt };
    return helper.columns(COLUMNS[variant].map((id) => all[id as keyof typeof all]));
  }, [t, tRuntime, variant, viewer]);

  const turns = (run: Run) => {
    const session = sessionOf(run);
    return session?.open && hasOtherTurns(run) ? <SessionTurns run={run} id={session.controls} /> : null;
  };
  return (
    <SessionRows.Provider value={sessionOf}>
      <DataTable
        data={runs}
        columns={columns}
        caption={caption}
        getRowId={(row) => (grouped && isSessionTurn(row) ? `session-${row.session_run_id}` : String(row.id))}
        columnClassNames={NARROW_HIDDEN[variant]}
        testId={testId}
        mobile={mobile}
        detail={grouped ? turns : undefined}
      />
    </SessionRows.Provider>
  );
}
