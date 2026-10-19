"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useTranslations } from "next-intl";

import { When } from "@/components/data/when";
import { PageHeader } from "@/components/shell/page-header";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { PageSkeleton } from "@/components/states/states";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";

import { sprintsQuery, type TaskDetail, taskQuery, tasksPeopleQuery, type TasksSettings, tasksSettingsQuery } from "./api";
import { PriorityTag, TaskRef, TaskStatusBadge, TypeTag } from "./badges";
import { ReaderNote } from "./chrome";
import { useTaskEdit, useTasksViewer } from "./hooks";
import { boardHref, taskHref } from "./links";
import { canWrite, statusOf } from "./model";
import { TaskActivity } from "./task-activity";
import { StatusSelect } from "./task-controls";
import { TaskDescription } from "./task-description";
import { TaskDetails } from "./task-details";
import { TaskLinks } from "./task-links";

/** Where an import took the task from, as its external_ref names it: a system, an id and maybe a page. */
function sourceOf(task: TaskDetail): { system: string; id: string; url: string | null } | null {
  const ref = task.external_ref;
  if (!ref || typeof ref.system !== "string" || typeof ref.id !== "string") return null;
  return { system: ref.system, id: ref.id, url: typeof ref.url === "string" && /^https?:\/\//i.test(ref.url) ? ref.url : null };
}

function TaskSub({ project, task }: { project: string; task: TaskDetail }) {
  const t = useTranslations("tasks.page");
  const source = sourceOf(task);
  const link = "text-brand underline underline-offset-4 hover:text-brand-hover";
  return (
    <>
      {t("created", { login: task.created_by })} <When value={task.created_at} />.{" "}
      {task.sprint ? (
        <>
          {t("inSprint")}{" "}
          <Link href={boardHref(project, { sprint: task.sprint })} className={link}>
            {task.sprint}
          </Link>
          .
        </>
      ) : (
        t("noSprint")
      )}
      {task.parent ? (
        <>
          {" "}
          {t("partOf")}{" "}
          <Link href={taskHref(project, task.parent)} className={`${link} font-mono`}>
            {task.parent}
          </Link>
          .
        </>
      ) : null}
      {source ? (
        <>
          {" "}
          {source.url ? (
            <a href={source.url} target="_blank" rel="noopener noreferrer" className={link}>
              {t("importedFrom", { system: source.system, id: source.id })}
            </a>
          ) : (
            t("importedFrom", { system: source.system, id: source.id })
          )}
          .
        </>
      ) : null}
    </>
  );
}

function TaskView({ project, task, settings }: { project: string; task: TaskDetail; settings: TasksSettings }) {
  const t = useTranslations("tasks.page");
  const viewer = useTasksViewer(project);
  const writer = canWrite(viewer);
  const people = useQuery({ ...tasksPeopleQuery(browserApi, project), enabled: writer });
  const sprints = useQuery({ ...sprintsQuery(browserApi, project), enabled: writer });
  const edit = useTaskEdit(project, settings);
  const status = statusOf(settings, task.status);
  return (
    <div className="flex flex-col gap-6" data-testid="task-page" data-task={task.ref}>
      <PageHeader
        leading={<TaskRef refName={task.ref} testId="task-ref" />}
        title={task.title}
        status={<TaskStatusBadge name={status?.name ?? task.status} group={task.status_group} size="lg" />}
        tags={
          <>
            <TypeTag type={task.type} />
            <PriorityTag priority={task.priority} />
          </>
        }
        sub={<TaskSub project={project} task={task} />}
        actions={writer ? <StatusSelect task={task} settings={settings} edit={edit} size="default" className="w-44" testId="task-page-status" /> : null}
        testId="task-head"
      />
      {viewer && !writer ? <ReaderNote project={project}>{t("readerText")}</ReaderNote> : null}
      <div className="grid min-w-0 gap-6 lg:grid-cols-[minmax(0,1fr)_20rem]">
        <div className="flex min-w-0 flex-col gap-6">
          <TaskDescription project={project} task={task} writer={writer} />
          <TaskLinks project={project} task={task} settings={settings} writer={writer} />
          <TaskActivity project={project} task={task} settings={settings} writer={writer} />
        </div>
        <div className="min-w-0">
          <TaskDetails project={project} task={task} settings={settings} people={people.data ?? []} sprints={sprints.data ?? []} writer={writer} />
        </div>
      </div>
    </div>
  );
}

/**
 * A task whole (/p/{project}/x/tasks/board/{ref}): its head with its status, type and priority, where it stands (who
 * made it, its sprint, its parent, the source it was imported from), its description, links, history and comments,
 * and its details beside them. A writer changes everything here; a reader reads.
 */
export function TaskPage({ project, taskRef, initialError }: { project: string; taskRef: string; initialError: ApiErrorInfo | null }) {
  const settings = useHubQuery(tasksSettingsQuery(browserApi, project), initialError);
  const task = useHubQuery(taskQuery(browserApi, project, taskRef));
  return (
    <QueryView state={settings} loading={<PageSkeleton />}>
      {(config) => (
        <QueryView state={task} loading={<PageSkeleton />}>
          {(found) => <TaskView project={project} task={found} settings={config} />}
        </QueryView>
      )}
    </QueryView>
  );
}
