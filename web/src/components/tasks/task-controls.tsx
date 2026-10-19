"use client";

import { useTranslations } from "next-intl";
import { useState } from "react";

import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select";
import { cn } from "@/lib/utils";

import type { Sprint, Task, TaskDetail, TaskEdit, TaskPerson, TasksSettings } from "./api";
import { sprintChoices, statusOf } from "./model";

/** Change a task and say `done` once the hub took it; what `useTaskEdit` returns. */
export type EditTask = (task: Pick<Task, "ref" | "number">, change: TaskEdit, done?: string) => Promise<TaskDetail | null>;

const MANY = "__many__"; // the assignee select's value while a task has more than one

/** A select that writes as it changes, disabled while its write runs. */
function useWrite() {
  const [busy, setBusy] = useState(false);
  const run = async (write: () => Promise<unknown>) => {
    setBusy(true);
    try {
      await write();
    } finally {
      setBusy(false);
    }
  };
  return { busy, run };
}

type ControlProps = { task: Task; edit: EditTask; className?: string; size?: "sm" | "default"; testId?: string };

/** A task's status, from the project's list; a status the config no longer lists stays shown, not chosen again. */
export function StatusSelect({ task, settings, edit, className, size = "sm", testId = "task-status-select" }: ControlProps & { settings: TasksSettings }) {
  const t = useTranslations("tasks.controls");
  const { busy, run } = useWrite();
  const known = statusOf(settings, task.status) !== null;
  return (
    <NativeSelect
      size={size}
      value={task.status}
      aria-label={t("status", { ref: task.ref })}
      aria-busy={busy || undefined}
      disabled={busy}
      onChange={(event) => {
        const key = event.target.value;
        const name = statusOf(settings, key)?.name ?? key;
        void run(() => edit(task, { status: key }, t("moved", { ref: task.ref, status: name })));
      }}
      className={cn("w-full", className)}
      data-testid={testId}
    >
      {known ? null : (
        <NativeSelectOption value={task.status} disabled>
          {task.status}
        </NativeSelectOption>
      )}
      {settings.statuses.map((status) => (
        <NativeSelectOption key={status.key} value={status.key}>
          {status.name}
        </NativeSelectOption>
      ))}
    </NativeSelect>
  );
}

/**
 * A task's assignee: Unassigned or one member. A task with more than one shows them as one choice, kept until another
 * is picked, which replaces them all; the task's page changes each one.
 */
export function AssigneeSelect({ task, people, edit, className, size = "sm", testId = "task-assignee-select" }: ControlProps & { people: readonly TaskPerson[] }) {
  const t = useTranslations("tasks.controls");
  const { busy, run } = useWrite();
  const many = task.assignees.length > 1;
  const value = many ? MANY : (task.assignees[0] ?? "");
  const logins = [...new Set([...people.map((person) => person.login), ...task.assignees])];
  return (
    <NativeSelect
      size={size}
      value={value}
      aria-label={t("assignee", { ref: task.ref })}
      aria-busy={busy || undefined}
      disabled={busy}
      onChange={(event) => {
        const login = event.target.value;
        if (login === MANY) return;
        const done = login ? t("assigned", { ref: task.ref, login }) : t("unassigned", { ref: task.ref });
        void run(() => edit(task, { assignees: login ? [login] : [] }, done));
      }}
      className={cn("w-full", className)}
      data-testid={testId}
    >
      {many ? (
        <NativeSelectOption value={MANY} disabled>
          {task.assignees.join(", ")}
        </NativeSelectOption>
      ) : null}
      <NativeSelectOption value="">{t("nobody")}</NativeSelectOption>
      {logins.map((login) => (
        <NativeSelectOption key={login} value={login}>
          {login}
        </NativeSelectOption>
      ))}
    </NativeSelect>
  );
}

/** A task's sprint: a sprint not closed, or none (the backlog). */
export function SprintSelect({ task, sprints, edit, className, size = "sm", testId = "task-sprint-select" }: ControlProps & { sprints: readonly Sprint[] }) {
  const t = useTranslations("tasks.controls");
  const { busy, run } = useWrite();
  return (
    <NativeSelect
      size={size}
      value={task.sprint ?? ""}
      aria-label={t("sprint", { ref: task.ref })}
      aria-busy={busy || undefined}
      disabled={busy}
      onChange={(event) => {
        const name = event.target.value || null;
        const done = name ? t("movedToSprint", { ref: task.ref, sprint: name }) : t("movedToBacklog", { ref: task.ref });
        void run(() => edit(task, { sprint: name }, done));
      }}
      className={cn("w-full", className)}
      data-testid={testId}
    >
      <NativeSelectOption value="">{t("noSprint")}</NativeSelectOption>
      {sprintChoices(sprints, task.sprint).map((sprint) => (
        <NativeSelectOption key={sprint.name} value={sprint.name}>
          {sprint.name}
        </NativeSelectOption>
      ))}
    </NativeSelect>
  );
}
