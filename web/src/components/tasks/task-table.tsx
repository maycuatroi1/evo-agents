"use client";

import Link from "next/link";
import { useTranslations } from "next-intl";
import { useMemo } from "react";

import { CellMain, DataTable, dataTableColumns } from "@/components/data/data-table";
import { NAME_LINK } from "@/components/data/identifier";

import type { Sprint, Task } from "./api";
import { Assignees, BlockedBadge, CodeChips, PriorityTag, StepsDone, TaskStatusBadge } from "./badges";
import { taskHref } from "./links";
import { partsOf } from "./model";
import { DueDate } from "./task-card";
import { type EditTask, SprintSelect } from "./task-controls";

const PRIORITY_ORDER = { urgent: 0, high: 1, medium: 2, low: 3 } as const;
const NARROW_HIDDEN = { links: "hidden lg:table-cell", due_on: "hidden md:table-cell", assignees: "hidden md:table-cell", parts: "hidden sm:table-cell" };

/**
 * Tasks as one table (the backlog's groups, the visitor's own tasks): the ref and title, status, priority, assignees,
 * the SRS codes and plan steps it links, its due date and its sprint, which a writer changes in place when `sprints` is
 * given. With `login`, a column says which slots of each task are theirs. Under 768 px each task is a row of a list
 * that opens its page.
 */
export function TaskTable({
  project,
  tasks,
  caption,
  statusName,
  login,
  sprints,
  edit,
  testId,
}: {
  project: string;
  tasks: Task[];
  caption: string;
  statusName: (key: string) => string;
  login?: string | null;
  sprints?: readonly Sprint[] | null;
  edit?: EditTask | null;
  testId: string;
}) {
  const t = useTranslations("tasks.table");
  const tPriority = useTranslations("tasks.priority");
  const tPart = useTranslations("tasks.part");
  const columns = useMemo(() => {
    const helper = dataTableColumns<Task>();
    return helper.columns([
      helper.accessor("number", {
        header: () => t("task"),
        sortFn: "basic",
        meta: { primary: true },
        cell: (info) => {
          const task = info.row.original;
          return (
            <CellMain sub={<span className="font-mono">{task.ref}</span>} subTitle={`${task.ref}, ${task.type}`}>
              <Link href={taskHref(project, task.ref)} className={`${NAME_LINK} truncate`} data-task-link={task.ref}>
                {task.title}
              </Link>
            </CellMain>
          );
        },
      }),
      helper.accessor("status", {
        header: () => t("status"),
        cell: (info) => (
          <span className="flex flex-wrap items-center gap-1.5">
            <TaskStatusBadge name={statusName(info.getValue())} group={info.row.original.status_group} />
            {info.row.original.blocked ? <BlockedBadge /> : null}
          </span>
        ),
      }),
      ...(login
        ? [
            helper.display({
              id: "parts",
              header: () => t("parts"),
              cell: (info) => <span className="text-xs">{partsOf(info.row.original, login).map((part) => tPart(part)).join(", ")}</span>,
            }),
          ]
        : []),
      helper.accessor((task) => PRIORITY_ORDER[task.priority], {
        id: "priority",
        header: () => t("priority"),
        sortFn: "basic",
        cell: (info) => <PriorityTag priority={info.row.original.priority} />,
      }),
      helper.accessor((task) => task.assignees.join(", "), {
        id: "assignees",
        header: () => t("assignees"),
        cell: (info) => <Assignees logins={info.row.original.assignees} />,
      }),
      helper.display({
        id: "links",
        header: () => t("links"),
        cell: (info) => (
          <span className="flex items-center gap-1.5">
            <CodeChips codes={info.row.original.codes} max={2} />
            <StepsDone done={info.row.original.steps_done} total={info.row.original.steps_total} />
          </span>
        ),
      }),
      helper.accessor((task) => task.due_on ?? "", {
        id: "due_on",
        header: () => t("due"),
        sortFn: "alphanumeric",
        cell: (info) => <DueDate task={info.row.original} />,
      }),
      helper.accessor((task) => task.sprint ?? "", {
        id: "sprint",
        header: () => t("sprint"),
        cell: (info) => {
          const task = info.row.original;
          if (sprints && edit) return <SprintSelect task={task} sprints={sprints} edit={edit} className="min-w-36" />;
          return <span className="text-xs">{task.sprint ?? t("noSprint")}</span>;
        },
      }),
    ]);
  }, [edit, login, project, sprints, statusName, t, tPart]);
  return (
    <DataTable
      data={tasks}
      columns={columns}
      caption={caption}
      getRowId={(task) => task.ref}
      columnClassNames={NARROW_HIDDEN}
      testId={testId}
      mobile={(task) => ({
        title: task.title,
        titleText: task.title,
        href: taskHref(project, task.ref),
        status: (
          <span className="flex flex-wrap items-center justify-end gap-1.5">
            <TaskStatusBadge name={statusName(task.status)} group={task.status_group} />
            {task.blocked ? <BlockedBadge /> : null}
          </span>
        ),
        meta: [task.ref, tPriority(task.priority), task.assignees.join(", ") || null, task.codes.join(", ") || null, login ? partsOf(task, login).map((part) => tPart(part)).join(", ") : null]
          .filter(Boolean)
          .join(", "),
        data: { task: task.ref },
      })}
    />
  );
}
