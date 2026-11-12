"use client";

import { AlarmClock, Plus } from "lucide-react";
import { usePathname, useSearchParams } from "next/navigation";
import { useTranslations } from "next-intl";
import { useMemo, useState } from "react";

import { DataCard } from "@/components/data/data-card";
import { CellMain, DataTable, dataTableColumns } from "@/components/data/data-table";
import { useMoney } from "@/components/data/facts";
import { PageHeader } from "@/components/shell/page-header";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { EmptyState, TableSkeleton } from "@/components/states/states";
import { Button } from "@/components/ui/button";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";

import { FiresSheet } from "./fires-sheet";
import { useCronWriter } from "./hooks";
import { type CronAction, DeleteDialog, JobMenu, PauseDialog, useQuickActions } from "./job-actions";
import { CronJobDialog } from "./job-dialog";
import { KindTag, LastFire, PausedBadge, Relative, ScheduleLine, workText } from "./parts";
import { type CronJob, cronJobsQuery } from "./queries";

const NARROW_HIDDEN = { last: "hidden md:table-cell", cost: "hidden lg:table-cell" };

/** When the job fires next, or, paused, Paused and why: the one state the list marks, since a running job needs none. */
function NextFire({ job }: { job: CronJob }) {
  const t = useTranslations("cron.list");
  if (job.paused) {
    const why = job.paused_reason ?? (job.paused_by ? t("pausedBy", { login: job.paused_by }) : t("pausedByHub"));
    return (
      <span className="flex min-w-0 flex-col items-start gap-0.5" data-testid="cron-next-paused">
        <PausedBadge />
        <span className="block max-w-[28ch] truncate text-xs text-fg-subtle" title={why}>
          {why}
        </span>
      </span>
    );
  }
  return job.next_fire_at ? <Relative value={job.next_fire_at} /> : <span className="text-fg-subtle">-</span>;
}

/** The project's jobs: name and what each starts, schedule, next fire, last fire with its run, cost in 24 hours, actions. */
function JobsTable({ project, jobs, onAction }: { project: string; jobs: CronJob[]; onAction: (job: CronJob, action: CronAction) => void }) {
  const t = useTranslations("cron.list");
  const money = useMoney();
  const columns = useMemo(() => {
    const helper = dataTableColumns<CronJob>();
    return helper.columns([
      helper.accessor("name", {
        header: () => t("columns.name"),
        meta: { primary: true },
        cell: (info) => {
          const job = info.row.original;
          return (
            <CellMain sub={workText(job)} subTitle={workText(job)}>
              <button
                type="button"
                onClick={() => onAction(job, "history")}
                className="min-w-0 cursor-pointer truncate text-left font-mono underline-offset-4 hover:underline focus-visible:underline focus-visible:outline-none"
                data-testid="cron-job-link"
                data-job={job.name}
              >
                {job.name}
              </button>
              <KindTag kind={job.kind} />
            </CellMain>
          );
        },
      }),
      helper.display({ id: "schedule", header: () => t("columns.schedule"), cell: (info) => <ScheduleLine job={info.row.original} /> }),
      helper.display({ id: "next", header: () => t("columns.next"), cell: (info) => <NextFire job={info.row.original} /> }),
      helper.display({ id: "last", header: () => t("columns.last"), cell: (info) => <LastFire project={project} job={info.row.original} /> }),
      helper.accessor("cost_24h_usd", {
        id: "cost",
        header: () => t("columns.cost"),
        meta: { numeric: true },
        cell: (info) => <span className="tabular-nums">{money(info.getValue())}</span>,
      }),
      helper.display({
        id: "actions",
        header: () => <span className="sr-only">{t("columns.actions")}</span>,
        meta: { actions: true },
        cell: (info) => <JobMenu job={info.row.original} onAction={(action) => onAction(info.row.original, action)} />,
      }),
    ]);
  }, [t, project, money, onAction]);
  return (
    <DataTable
      data={jobs}
      columns={columns}
      caption={t("caption", { project })}
      getRowId={(job) => job.name}
      initialSorting={[{ id: "name", desc: false }]}
      columnClassNames={NARROW_HIDDEN}
      testId="cron-jobs-table"
      mobile={(job) => ({
        title: job.name,
        titleClassName: "font-mono",
        tags: <KindTag kind={job.kind} />,
        status: job.paused ? <PausedBadge /> : job.next_fire_at ? <Relative value={job.next_fire_at} /> : null,
        meta: workText(job),
        metaText: workText(job),
        actions: <JobMenu job={job} onAction={(action) => onAction(job, action)} />,
        data: { job: job.name },
      })}
    />
  );
}

type Open = { action: Exclude<CronAction, "run" | "resume">; name: string } | { action: "new" } | null;

/** The open dialog or sheet, kept in the address for the history (`?job=`) so a link opens a job's fires. */
function useOpen(): [Open, (next: Open) => void] {
  const params = useSearchParams();
  const pathname = usePathname();
  const [open, setOpen] = useState<Open>(() => {
    const name = params.get("job");
    return name ? { action: "history", name } : null;
  });
  const set = (next: Open) => {
    setOpen(next);
    const job = next && next.action === "history" ? next.name : null;
    window.history.replaceState(null, "", `${pathname}${job ? `?${new URLSearchParams({ job })}` : ""}`);
  };
  return [open, set];
}

function Jobs({ project, jobs, writer, onNew, onAction }: { project: string; jobs: CronJob[]; writer: boolean; onNew: () => void; onAction: (job: CronJob, action: CronAction) => void }) {
  const t = useTranslations("cron.list");
  if (jobs.length === 0) {
    return (
      <EmptyState icon={AlarmClock} title={t("emptyTitle")} description={t("emptyText")} testId="cron-jobs-empty">
        {writer ? (
          <Button type="button" onClick={onNew} data-testid="cron-new-empty">
            <Plus aria-hidden="true" />
            {t("new")}
          </Button>
        ) : (
          <code className="rounded-xs bg-muted px-2 py-1 font-mono text-xs text-foreground">evo-agents hub cron set</code>
        )}
      </EmptyState>
    );
  }
  return (
    <DataCard testId="cron-jobs-card">
      <JobsTable project={project} jobs={jobs} onAction={onAction} />
    </DataCard>
  );
}

/**
 * A project's cron jobs (/p/{project}/cron-jobs), for any member: each job's schedule in words and as written, its
 * next fire or why it is paused, its last fire with the run it started, and what its runs cost in 24 hours. A writer
 * makes a job here (New cron job) or with `evo-agents hub cron set`; its owner edits it; its owner or an admin runs it
 * now, pauses, resumes or deletes it. The menu offers only what the visitor may do. A job's fires open in a sheet.
 */
export function CronJobsPage({ project, initialError }: { project: string; initialError: ApiErrorInfo | null }) {
  const t = useTranslations("cron.list");
  const state = useHubQuery(cronJobsQuery(browserApi, project), initialError);
  const writer = useCronWriter(project);
  const quick = useQuickActions(project);
  const [open, setOpen] = useOpen();
  const jobs = state.status === "success" ? state.data : [];
  // The job is looked up by name in the list as last read, so a dialog shows the revision the hub holds now.
  const held = open && open.action !== "new" ? (jobs.find((job) => job.name === open.name) ?? null) : null;
  const close = () => setOpen(null);
  const act = (job: CronJob, action: CronAction) => {
    if (action === "run") quick.runNow(job);
    else if (action === "resume") quick.resume(job);
    else setOpen({ action, name: job.name });
  };
  return (
    <div className="flex min-w-0 flex-col gap-6" data-testid="cron-jobs-page">
      <PageHeader
        title={t("title")}
        sub={t("sub")}
        actions={
          writer && jobs.length > 0 ? (
            <Button type="button" onClick={() => setOpen({ action: "new" })} data-testid="cron-new">
              <Plus aria-hidden="true" />
              {t("new")}
            </Button>
          ) : null
        }
      />
      <QueryView state={state} loading={<TableSkeleton rows={3} />}>
        {(list) => <Jobs project={project} jobs={list} writer={writer} onNew={() => setOpen({ action: "new" })} onAction={act} />}
      </QueryView>
      {writer ? (
        <CronJobDialog
          project={project}
          job={open?.action === "edit" ? held : null}
          open={open?.action === "new" || (open?.action === "edit" && held !== null)}
          onOpenChange={(next) => (next ? undefined : close())}
        />
      ) : null}
      {open?.action === "pause" && held ? <PauseDialog key={held.name} project={project} job={held} onClose={close} /> : null}
      {open?.action === "delete" && held ? <DeleteDialog key={held.name} project={project} job={held} onClose={close} /> : null}
      <FiresSheet project={project} job={open?.action === "history" ? held : null} onClose={close} />
    </div>
  );
}
