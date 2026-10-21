"use client";

import { CalendarPlus, ChevronRight, ListChecks, Play, Plus } from "lucide-react";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { type ReactNode, useState } from "react";

import { DataCard } from "@/components/data/data-card";
import { useWriteFailure } from "@/components/feedback/notice";
import { notify, notifyFailure } from "@/components/feedback/toast";
import { PageHeader } from "@/components/shell/page-header";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { EmptyState, TableSkeleton } from "@/components/states/states";
import { Button } from "@/components/ui/button";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";

import { editSprint, type Sprint, sprintsQuery, type Task, tasksQuery, type TasksSettings, tasksSettingsQuery } from "./api";
import { SprintBadge, useSprintDays } from "./badges";
import { ReaderNote, TasksTabs } from "./chrome";
import { signInAgainOn401, useTaskEdit, useTasksRefresh, useTasksViewer } from "./hooks";
import { boardHref, newTaskHref } from "./links";
import { canWrite, orderedSprints, statusOf } from "./model";
import { type SprintDialogFor, SprintDialog } from "./sprint-dialogs";
import { TaskTable } from "./task-table";

const ALL = { kind: "all" } as const;

function StartSprint({ project, sprint }: { project: string; sprint: Sprint }) {
  const t = useTranslations("tasks.backlog");
  const failure = useWriteFailure();
  const refresh = useTasksRefresh(project);
  const [busy, setBusy] = useState(false);
  const start = async () => {
    setBusy(true);
    try {
      await editSprint(browserApi(), project, sprint.name, { state: "active" });
      await refresh();
      notify({ tone: "success", text: t("started", { sprint: sprint.name }) });
    } catch (error) {
      signInAgainOn401(error);
      notifyFailure(t("startFailed", { sprint: sprint.name }), failure(error));
    } finally {
      setBusy(false);
    }
  };
  return (
    <Button type="button" size="sm" variant="secondary" busy={busy} onClick={() => void start()} data-testid="sprint-start">
      <Play aria-hidden="true" />
      {t("start")}
    </Button>
  );
}

/** A group of the backlog: a sprint, or the tasks of none, with its tasks as one table. */
function Group({
  id,
  title,
  head,
  actions,
  tasks,
  table,
  testId,
}: {
  id: string;
  title: string;
  head?: ReactNode;
  actions?: ReactNode;
  tasks: Task[];
  table: (tasks: Task[], caption: string) => ReactNode;
  testId: string;
}) {
  const t = useTranslations("tasks.backlog");
  return (
    <section aria-labelledby={id} className="flex flex-col gap-2" data-testid={testId}>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
        <h2 id={id} className="text-[15px] leading-[22px] font-semibold [overflow-wrap:anywhere]">
          {title}
        </h2>
        {head}
        <span className="text-xs text-fg-subtle tabular-nums">{t("count", { count: tasks.length })}</span>
        {actions ? <div className="ml-auto flex flex-wrap items-center gap-2">{actions}</div> : null}
      </div>
      {tasks.length ? (
        <DataCard>{table(tasks, t("caption", { group: title }))}</DataCard>
      ) : (
        <p className="rounded-md border border-dashed px-4 py-5 text-center text-[13px] text-muted-foreground">{t("emptyGroup")}</p>
      )}
    </section>
  );
}

function BacklogBody({ project, settings, sprints, tasks, writer, onDialog }: { project: string; settings: TasksSettings; sprints: Sprint[]; tasks: Task[]; writer: boolean; onDialog: (open: SprintDialogFor) => void }) {
  const t = useTranslations("tasks.backlog");
  const days = useSprintDays();
  const edit = useTaskEdit(project, settings);
  const statusName = (key: string) => statusOf(settings, key)?.name ?? key;
  const table = (shown: Task[], caption: string) => (
    <TaskTable project={project} tasks={shown} caption={caption} statusName={statusName} sprints={writer ? sprints : null} edit={writer ? edit : null} testId="tasks-table" />
  );
  const ordered = orderedSprints(sprints);
  const open = ordered.filter((sprint) => sprint.state !== "closed");
  const closed = ordered.filter((sprint) => sprint.state === "closed");
  const noActive = !sprints.some((sprint) => sprint.state === "active");
  const of = (sprint: Sprint | null) => tasks.filter((task) => task.sprint === (sprint?.name ?? null));
  const sprintHead = (sprint: Sprint) => (
    <>
      <SprintBadge state={sprint.state} />
      {days(sprint) ? <span className="text-xs text-muted-foreground">{days(sprint)}</span> : null}
      <Link href={boardHref(project, { sprint: sprint.name })} className="text-xs text-brand underline underline-offset-4 hover:text-brand-hover">
        {t("openBoard")}
      </Link>
    </>
  );
  return (
    <div className="flex flex-col gap-8">
      {open.map((sprint, index) => (
        <Group
          key={sprint.name}
          id={`backlog-sprint-${index}`}
          title={sprint.name}
          head={sprintHead(sprint)}
          tasks={of(sprint)}
          table={table}
          testId="backlog-sprint"
          actions={
            writer ? (
              sprint.state === "active" ? (
                <Button type="button" size="sm" variant="secondary" onClick={() => onDialog({ kind: "close", sprint })} data-testid="sprint-close">
                  {t("close")}
                </Button>
              ) : noActive ? (
                <StartSprint project={project} sprint={sprint} />
              ) : null
            ) : null
          }
        />
      ))}
      <Group id="backlog-none" title={t("noSprint")} tasks={of(null)} table={table} testId="backlog-none" />
      {closed.length ? (
        <details className="group flex flex-col gap-4" data-testid="backlog-closed">
          <summary className="inline-flex min-h-11 cursor-pointer list-none items-center gap-2 text-[15px] font-semibold [&::-webkit-details-marker]:hidden">
            <ChevronRight className="size-4 text-muted-foreground transition-transform group-open:rotate-90" aria-hidden="true" />
            {t("closed", { count: closed.length })}
          </summary>
          <div className="mt-4 flex flex-col gap-8">
            {closed.map((sprint, index) => (
              <Group key={sprint.name} id={`backlog-closed-${index}`} title={sprint.name} head={sprintHead(sprint)} tasks={of(sprint)} table={table} testId="backlog-sprint" />
            ))}
          </div>
        </details>
      ) : null}
    </div>
  );
}

/**
 * Every task of a project by sprint (/p/{project}/x/tasks/backlog): the active sprint, the planned ones by start, the
 * tasks of no sprint, then the closed sprints folded. A writer moves a task to another sprint from its row, adds a
 * sprint, starts a planned one while none is active, and closes the active one, moving its unfinished tasks on.
 */
export function BacklogPage({ project, title, initialError }: { project: string; title: string; initialError: ApiErrorInfo | null }) {
  const t = useTranslations("tasks.backlog");
  const viewer = useTasksViewer(project);
  const writer = canWrite(viewer);
  const settings = useHubQuery(tasksSettingsQuery(browserApi, project), initialError);
  const sprints = useHubQuery(sprintsQuery(browserApi, project));
  const tasks = useHubQuery(tasksQuery(browserApi, project, ALL));
  const allSprints = sprints.status === "success" ? sprints.data : [];
  const [dialog, setDialog] = useState<SprintDialogFor>(null);
  return (
    <div className="flex flex-col gap-6" data-testid="tasks-backlog-page">
      <div className="flex flex-col gap-4">
        <PageHeader
          title={title}
          actions={
            writer ? (
              <>
                <Button type="button" variant="secondary" onClick={() => setDialog({ kind: "new" })} data-testid="sprint-new">
                  <CalendarPlus aria-hidden="true" />
                  {t("newSprint")}
                </Button>
                <Button asChild>
                  <Link href={newTaskHref(project)} data-testid="tasks-new">
                    <Plus aria-hidden="true" />
                    {t("newTask")}
                  </Link>
                </Button>
              </>
            ) : null
          }
        />
        <TasksTabs project={project} current="backlog" writer={writer} />
      </div>
      {viewer && !writer ? <ReaderNote project={project}>{t("readerText")}</ReaderNote> : null}
      <QueryView state={settings} loading={<TableSkeleton rows={6} />}>
        {(config) => (
          <QueryView state={sprints} loading={<TableSkeleton rows={6} />}>
            {(known) => (
              <QueryView state={tasks} loading={<TableSkeleton rows={6} />}>
                {(all) =>
                  all.length === 0 && known.length === 0 ? (
                    <EmptyState icon={ListChecks} title={t("emptyTitle")} description={writer ? t("emptyWriter") : t("emptyReader")} testId="tasks-backlog-empty">
                      {writer ? (
                        <Button asChild>
                          <Link href={newTaskHref(project)}>
                            <Plus aria-hidden="true" />
                            {t("newTask")}
                          </Link>
                        </Button>
                      ) : null}
                    </EmptyState>
                  ) : (
                    <BacklogBody project={project} settings={config} sprints={known} tasks={all} writer={writer} onDialog={setDialog} />
                  )
                }
              </QueryView>
            )}
          </QueryView>
        )}
      </QueryView>
      {writer ? <SprintDialog project={project} sprints={allSprints} open={dialog} onOpenChange={setDialog} /> : null}
    </div>
  );
}
