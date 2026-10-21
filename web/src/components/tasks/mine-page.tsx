"use client";

import { ClipboardList, Columns3 } from "lucide-react";
import Link from "next/link";
import { useTranslations } from "next-intl";

import { DataCard } from "@/components/data/data-card";
import { PageHeader } from "@/components/shell/page-header";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { EmptyState, TableSkeleton } from "@/components/states/states";
import { Button } from "@/components/ui/button";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";

import { type Task, tasksQuery, type TasksSettings, tasksSettingsQuery } from "./api";
import { TasksTabs } from "./chrome";
import { useTasksViewer } from "./hooks";
import { boardHref } from "./links";
import { canWrite, isFinished, statusOf } from "./model";
import { TaskTable } from "./task-table";

const MINE = { kind: "mine" } as const;

function Part({ id, title, tasks, project, settings, login, empty }: { id: string; title: string; tasks: Task[]; project: string; settings: TasksSettings; login: string; empty: string }) {
  const t = useTranslations("tasks.mine");
  return (
    <section aria-labelledby={id} className="flex flex-col gap-2" data-testid={id}>
      <div className="flex items-center gap-3">
        <h2 id={id} className="text-[15px] leading-[22px] font-semibold">
          {title}
        </h2>
        <span className="text-xs text-fg-subtle tabular-nums">{t("count", { count: tasks.length })}</span>
      </div>
      {tasks.length ? (
        <DataCard>
          <TaskTable
            project={project}
            tasks={tasks}
            caption={title}
            statusName={(key) => statusOf(settings, key)?.name ?? key}
            login={login}
            testId={`${id}-table`}
          />
        </DataCard>
      ) : (
        <p className="rounded-md border border-dashed px-4 py-5 text-center text-[13px] text-muted-foreground">{empty}</p>
      )}
    </section>
  );
}

/**
 * The visitor's own tasks in a project (/p/{project}/x/tasks/mine): every task where they are an assignee or hold the
 * BA, QA or Dev slot, the open ones first, then those done or cancelled, with which slots are theirs.
 */
export function MinePage({ project, title, initialError }: { project: string; title: string; initialError: ApiErrorInfo | null }) {
  const t = useTranslations("tasks.mine");
  const viewer = useTasksViewer(project);
  const settings = useHubQuery(tasksSettingsQuery(browserApi, project), initialError);
  const tasks = useHubQuery(tasksQuery(browserApi, project, MINE));
  return (
    <div className="flex flex-col gap-6" data-testid="tasks-mine-page">
      <div className="flex flex-col gap-4">
        <PageHeader title={title} sub={viewer ? t("sub", { login: viewer.login }) : null} />
        <TasksTabs project={project} current="mine" writer={canWrite(viewer)} />
      </div>
      <QueryView state={settings} loading={<TableSkeleton rows={4} />}>
        {(config) => (
          <QueryView state={tasks} loading={<TableSkeleton rows={4} />}>
            {(mine) =>
              mine.length === 0 ? (
                <EmptyState icon={ClipboardList} title={t("emptyTitle")} description={t("emptyText")} testId="tasks-mine-empty">
                  <Button asChild variant="secondary">
                    <Link href={boardHref(project)}>
                      <Columns3 aria-hidden="true" />
                      {t("openBoard")}
                    </Link>
                  </Button>
                </EmptyState>
              ) : (
                <div className="flex flex-col gap-8">
                  <Part
                    id="mine-open"
                    title={t("open")}
                    tasks={mine.filter((task) => !isFinished(task))}
                    project={project}
                    settings={config}
                    login={viewer?.login ?? ""}
                    empty={t("noneOpen")}
                  />
                  <Part
                    id="mine-finished"
                    title={t("finished")}
                    tasks={mine.filter(isFinished)}
                    project={project}
                    settings={config}
                    login={viewer?.login ?? ""}
                    empty={t("noneFinished")}
                  />
                </div>
              )
            }
          </QueryView>
        )}
      </QueryView>
    </div>
  );
}
