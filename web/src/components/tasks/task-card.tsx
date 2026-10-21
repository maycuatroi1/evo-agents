"use client";

import { BookText, Bug, CalendarClock, GripVertical, type LucideIcon, Shapes, SquareCheckBig } from "lucide-react";
import Link from "next/link";
import { useFormatter, useTranslations } from "next-intl";
import type { DragEvent } from "react";

import { cn } from "@/lib/utils";

import type { Task, TaskPerson, TasksSettings } from "./api";
import { Assignees, CodeChips, PriorityTag, StepsDone } from "./badges";
import { taskHref } from "./links";
import { isFinished } from "./model";
import { AssigneeSelect, type EditTask, StatusSelect } from "./task-controls";

const TYPE_ICONS: Record<string, LucideIcon> = { task: SquareCheckBig, bug: Bug, story: BookText };

/** Today as the hub's dates are written (YYYY-MM-DD), in the visitor's own day. */
export function today(): string {
  const now = new Date();
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;
}

/** A due date as a short day (`Oct 24`), in red with "overdue" said once it has passed on a task still open. */
export function DueDate({ task, className }: { task: Pick<Task, "due_on" | "status_group">; className?: string }) {
  const t = useTranslations("tasks.card");
  const format = useFormatter();
  if (!task.due_on) return null;
  const overdue = !isFinished(task) && task.due_on < today();
  const day = format.dateTime(new Date(`${task.due_on}T00:00:00Z`), { month: "short", day: "numeric", timeZone: "UTC" });
  return (
    <span
      className={cn("inline-flex shrink-0 items-center gap-1 text-xs whitespace-nowrap tabular-nums", overdue ? "font-medium text-danger" : "text-muted-foreground", className)}
      title={overdue ? t("overdue", { day }) : t("due", { day })}
      data-testid="task-due"
      data-overdue={overdue || undefined}
    >
      <CalendarClock className="size-3.5" aria-hidden="true" />
      <span className="sr-only">{overdue ? t("overdue", { day }) : t("due", { day })}</span>
      <span aria-hidden="true">{day}</span>
    </span>
  );
}

/** The BA, QA and Dev of a task in one line ("BA alice, QA bao"), or nothing when none is set. */
export function RolesLine({ task }: { task: Pick<Task, "roles"> }) {
  const t = useTranslations("tasks.role");
  const parts = (["ba", "qa", "dev"] as const).filter((role) => task.roles[role].length).map((role) => `${t(role)} ${task.roles[role].join(", ")}`);
  if (parts.length === 0) return null;
  return <span className="min-w-0 truncate text-xs text-muted-foreground">{parts.join(", ")}</span>;
}

/**
 * A task on the board (web/DESIGN.md, components/tasks): its ref, type and priority, its assignees' faces, its title
 * linking to its page, the SRS codes it implements and its plan steps done, its BA, QA and Dev and its due date. A
 * writer drags it to another column, or uses its Status and Assignee menus, which are the way by keyboard and touch.
 */
export function TaskCard({
  project,
  task,
  settings,
  people,
  writer,
  edit,
  dragging,
  onDragStart,
  onDragEnd,
}: {
  project: string;
  task: Task;
  settings: TasksSettings;
  people: readonly TaskPerson[];
  writer: boolean;
  edit: EditTask;
  dragging: boolean;
  onDragStart: (task: Task) => void;
  onDragEnd: () => void;
}) {
  const t = useTranslations("tasks.card");
  const TypeIcon = TYPE_ICONS[task.type] ?? Shapes;
  const start = (event: DragEvent<HTMLLIElement>) => {
    event.dataTransfer.setData("text/plain", task.ref);
    event.dataTransfer.effectAllowed = "move";
    onDragStart(task);
  };
  return (
    <li
      draggable={writer}
      onDragStart={writer ? start : undefined}
      onDragEnd={writer ? onDragEnd : undefined}
      className={cn(
        "flex min-w-0 flex-col gap-2 rounded-md border bg-card p-3 shadow-raised transition-colors",
        writer && "cursor-grab active:cursor-grabbing",
        dragging && "opacity-50",
      )}
      data-testid="task-card"
      data-task={task.ref}
      data-status={task.status}
    >
      <div className="flex min-w-0 items-center gap-1.5">
        {writer ? <GripVertical className="-ml-1 size-3.5 shrink-0 text-fg-subtle max-md:hidden" aria-hidden="true" /> : null}
        <TypeIcon className="size-3.5 shrink-0 text-muted-foreground" aria-hidden="true" />
        <span className="sr-only">{t("type", { type: task.type })}</span>
        <span className="font-mono text-xs font-medium text-muted-foreground tabular-nums">{task.ref}</span>
        <PriorityTag priority={task.priority} />
        <span className="ml-auto">
          <Assignees logins={task.assignees} compact />
        </span>
      </div>
      <Link
        href={taskHref(project, task.ref)}
        className="text-sm leading-snug font-medium text-pretty text-foreground transition-colors [overflow-wrap:anywhere] hover:text-brand"
        data-testid="task-card-title"
      >
        <span className="line-clamp-3">{task.title}</span>
      </Link>
      {task.codes.length || task.steps_total ? (
        <div className="flex min-w-0 flex-wrap items-center gap-1.5">
          <CodeChips codes={task.codes} max={3} />
          <StepsDone done={task.steps_done} total={task.steps_total} />
        </div>
      ) : null}
      {task.due_on || Object.values(task.roles).some((logins) => logins.length) ? (
        <div className="flex min-w-0 items-center gap-2">
          <RolesLine task={task} />
          <DueDate task={task} className="ml-auto" />
        </div>
      ) : null}
      {writer ? (
        <div className="flex flex-col gap-1.5 border-t pt-2">
          <StatusSelect task={task} settings={settings} edit={edit} />
          <AssigneeSelect task={task} people={people} edit={edit} />
        </div>
      ) : null}
    </li>
  );
}
