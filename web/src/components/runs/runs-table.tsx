"use client";

import Link from "next/link";
import { useFormatter, useTranslations } from "next-intl";
import { useMemo } from "react";

import { CellMain, DataTable, dataTableColumns } from "@/components/data/data-table";
import { Identifier, NAME_LINK, RunRef } from "@/components/data/identifier";
import { useNow } from "@/components/kg/use-now";
import { planHref, stepHref } from "@/components/plans/links";
import { StatusBadge } from "@/components/status/status-badge";
import { workerHref } from "@/components/workers/queries";
import { cn } from "@/lib/utils";

import { PlanRunKindBadge } from "./badges";
import { durationParts, runTiming } from "./model";
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
  project: ["run", "step", "state", "worker", "runtime", "started", "duration", "attempt"],
  step: ["run", "state", "worker", "dispatcher", "runtime", "started", "duration", "attempt"],
  worker: ["run", "step", "state", "runtime", "started", "duration", "attempt"],
};

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
 * The run's title and one line under it: its plan and step (and project on a worker's page), or, once it failed, was
 * lost or cancelled, the reason in danger. A plan run carries the Plan run tag after its title.
 */
function StepCell({ run, showProject }: { run: Run; showProject: boolean }) {
  const t = useTranslations("runs");
  const ended = run.state === "failed" || run.state === "lost" || run.state === "cancelled";
  const plan = run.kind === "plan" || run.step_key === null;
  const repos = run.repos?.length ?? 0;
  const where = plan
    ? showProject
      ? t("planLineProject", { project: run.project, plan: run.plan_id, repos })
      : t("planLine", { plan: run.plan_id, repos })
    : showProject
      ? t("stepLineProject", { project: run.project, plan: run.plan_id, step: run.step_key ?? "" })
      : t("stepLine", { plan: run.plan_id, step: run.step_key ?? "" });
  const failure = ended && run.error ? run.error : null;
  const title = plan ? (run.title ?? run.plan_id) : (run.title ?? t("untitled"));
  return (
    <CellMain
      sub={failure ?? where}
      subTitle={failure ? `${where}: ${failure}` : where}
      danger={failure !== null}
      subTestId={failure ? "run-error" : "run-where"}
    >
      <Link
        href={plan ? planHref(run.project, run.plan_id) : stepHref(run.project, run.plan_id, run.step_key ?? "")}
        className={cn(NAME_LINK, "truncate")}
        title={title}
        data-testid={plan ? "run-plan-link" : "run-step-link"}
      >
        {title}
      </Link>
      {plan ? <PlanRunKindBadge /> : null}
    </CellMain>
  );
}

function WorkerCell({ run, viewer }: { run: Run; viewer: Viewer | null }) {
  const t = useTranslations("runs");
  if (run.worker_id === null || !run.worker) {
    return (
      <span className="text-fg-subtle">{run.pinned_worker_id !== null ? t("pinnedWaiting") : t("noWorkerYet")}</span>
    );
  }
  const mayOpen = viewer !== null && (viewer.admin || viewer.login === run.dispatched_by);
  return <Identifier value={run.worker} href={mayOpen ? workerHref(run.worker_id) : undefined} />;
}

/** Runs as a table, newest first as the API sends them; the order is the server's, so the columns do not sort. */
export function RunsTable({
  runs,
  caption,
  viewer,
  variant = "project",
  testId = "runs-table",
}: {
  runs: Run[];
  caption: string;
  viewer: Viewer | null;
  variant?: RunsTableVariant;
  testId?: string;
}) {
  const t = useTranslations("runs");
  const tRuntime = useTranslations("runs.runtime");
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
      cell: (info) => <StatusBadge kind="run" status={info.row.original.state} />,
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
    const all = { run, step, state, worker, dispatcher, runtime, started, duration, attempt };
    return helper.columns(COLUMNS[variant].map((id) => all[id as keyof typeof all]));
  }, [t, tRuntime, variant, viewer]);

  return (
    <DataTable
      data={runs}
      columns={columns}
      caption={caption}
      getRowId={(row) => String(row.id)}
      columnClassNames={NARROW_HIDDEN[variant]}
      testId={testId}
    />
  );
}
