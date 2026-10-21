"use client";

import { X } from "lucide-react";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { type FormEvent, type ReactNode, useId, useState } from "react";

import { NAME_LINK } from "@/components/data/identifier";
import { VisibilityLevel } from "@/components/data/visibility";
import { useWriteFailure } from "@/components/feedback/notice";
import { notify, notifyFailure } from "@/components/feedback/toast";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select";

import { PRIORITIES, type Priority, type Sprint, type TaskDetail, type TaskEdit, type TaskPerson, type TasksSettings } from "./api";
import { PersonAvatar, PriorityTag, TypeTag } from "./badges";
import { signInAgainOn401, useTaskSave } from "./hooks";
import { taskHref } from "./links";
import { DueDate } from "./task-card";
import { SprintSelect } from "./task-controls";

type Save = (change: TaskEdit, done: string) => Promise<boolean>;

/** A field of the card: its name over its value or control, the name naming the control. */
function Field({ label, htmlFor, children, testId }: { label: string; htmlFor?: string; children: ReactNode; testId?: string }) {
  return (
    <div className="flex min-w-0 flex-col gap-1.5" data-testid={testId}>
      {htmlFor ? (
        <label htmlFor={htmlFor} className="text-xs font-medium text-muted-foreground">
          {label}
        </label>
      ) : (
        <span className="text-xs font-medium text-muted-foreground">{label}</span>
      )}
      {children}
    </div>
  );
}

/** The people in one slot of a task: each as a chip a writer removes, and a menu that adds a member to it. */
function PeopleField({ slot, logins, people, writer, onChange, testId }: { slot: string; logins: string[]; people: readonly TaskPerson[]; writer: boolean; onChange: (logins: string[]) => void; testId: string }) {
  const t = useTranslations("tasks.details");
  const id = useId();
  const others = people.filter((person) => !logins.includes(person.login));
  return (
    <div className="flex min-w-0 flex-col gap-1.5" data-testid={testId}>
      <span id={id} className="text-xs font-medium text-muted-foreground">
        {slot}
      </span>
      <ul className="flex flex-wrap gap-1.5" aria-labelledby={id}>
        {logins.length === 0 ? <li className="text-[13px] text-fg-subtle">{t("nobody")}</li> : null}
        {logins.map((login) => (
          <li key={login} className="inline-flex h-7 max-w-full items-center gap-1.5 rounded-full border bg-card pr-1 pl-1 text-[13px] max-md:h-11" data-login={login}>
            <PersonAvatar login={login} className="size-5" />
            <span className="min-w-0 truncate">{login}</span>
            {writer ? (
              <Button type="button" variant="ghost" size="icon-sm" className="size-5 rounded-full max-md:size-9" aria-label={t("remove", { login, slot })} onClick={() => onChange(logins.filter((one) => one !== login))}>
                <X aria-hidden="true" />
              </Button>
            ) : (
              <span className="w-1" />
            )}
          </li>
        ))}
      </ul>
      {writer && others.length ? (
        <NativeSelect
          size="sm"
          value=""
          aria-label={t("add", { slot })}
          onChange={(event) => {
            if (event.target.value) onChange([...logins, event.target.value]);
          }}
          className="w-full"
          data-testid={`${testId}-add`}
        >
          <NativeSelectOption value="">{t("addPlaceholder")}</NativeSelectOption>
          {others.map((person) => (
            <NativeSelectOption key={person.login} value={person.login}>
              {person.login}
            </NativeSelectOption>
          ))}
        </NativeSelect>
      ) : null}
    </div>
  );
}

function ParentField({ project, task, writer, save }: { project: string; task: TaskDetail; writer: boolean; save: Save }) {
  const t = useTranslations("tasks.details");
  const id = useId();
  const [text, setText] = useState(task.parent ?? "");
  if (!writer) {
    return (
      <Field label={t("parent")}>
        {task.parent ? (
          <Link href={taskHref(project, task.parent)} className={`${NAME_LINK} font-mono text-[13px]`}>
            {task.parent}
          </Link>
        ) : (
          <span className="text-[13px] text-fg-subtle">{t("none")}</span>
        )}
      </Field>
    );
  }
  const submit = (event: FormEvent) => {
    event.preventDefault();
    const ref = text.trim() || null;
    if (ref === task.parent) return;
    void save({ parent: ref }, ref ? t("parentSet", { ref: task.ref, parent: ref }) : t("parentCleared", { ref: task.ref }));
  };
  return (
    <Field label={t("parent")} htmlFor={id}>
      <form onSubmit={submit} className="flex gap-1.5">
        <Input id={id} value={text} onChange={(event) => setText(event.target.value)} placeholder={t("parentPlaceholder")} maxLength={20} className="min-w-0 flex-1 font-mono" data-testid="task-parent" />
        <Button type="submit" variant="secondary" disabled={(text.trim() || null) === task.parent}>
          {t("set")}
        </Button>
      </form>
    </Field>
  );
}

/**
 * A task's details beside its page: type, priority, sprint, due date, parent, its assignees and its BA, QA and Dev,
 * and its visibility. A writer changes each where it stands, saved as it changes; people are members with a grant on
 * the project, as the hub takes no one else.
 */
export function TaskDetails({ project, task, settings, people, sprints, writer }: { project: string; task: TaskDetail; settings: TasksSettings; people: readonly TaskPerson[]; sprints: readonly Sprint[]; writer: boolean }) {
  const t = useTranslations("tasks.details");
  const tPriority = useTranslations("tasks.priority");
  const tRole = useTranslations("tasks.role");
  const ids = useId();
  const saveTask = useTaskSave(project);
  const failure = useWriteFailure();
  const save: Save = async (change, done) => {
    try {
      await saveTask(task.ref, change);
      notify({ tone: "success", text: done });
      return true;
    } catch (error) {
      signInAgainOn401(error);
      notifyFailure(t("failed", { ref: task.ref }), failure(error));
      return false;
    }
  };
  const level = typeof task.label.level === "string" ? task.label.level : null;
  const types = settings.types.includes(task.type) ? settings.types : [task.type, ...settings.types];
  return (
    <Card size="sm" className="h-fit" data-testid="task-details">
      <CardHeader>
        <CardTitle>
          <h2>{t("heading")}</h2>
        </CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <Field label={t("type")} htmlFor={writer ? `${ids}-type` : undefined}>
          {writer ? (
            <NativeSelect id={`${ids}-type`} value={task.type} onChange={(event) => void save({ type: event.target.value }, t("typeSet", { ref: task.ref, type: event.target.value }))} className="w-full" data-testid="task-type">
              {types.map((type) => (
                <NativeSelectOption key={type} value={type}>
                  {type}
                </NativeSelectOption>
              ))}
            </NativeSelect>
          ) : (
            <TypeTag type={task.type} />
          )}
        </Field>
        <Field label={t("priority")} htmlFor={writer ? `${ids}-priority` : undefined}>
          {writer ? (
            <NativeSelect
              id={`${ids}-priority`}
              value={task.priority}
              onChange={(event) => {
                const priority = event.target.value as Priority;
                void save({ priority }, t("prioritySet", { ref: task.ref, priority: tPriority(priority) }));
              }}
              className="w-full"
              data-testid="task-priority"
            >
              {PRIORITIES.map((priority) => (
                <NativeSelectOption key={priority} value={priority}>
                  {tPriority(priority)}
                </NativeSelectOption>
              ))}
            </NativeSelect>
          ) : (
            <PriorityTag priority={task.priority} />
          )}
        </Field>
        <Field label={t("sprint")}>
          {writer ? (
            <SprintSelect task={task} sprints={sprints} edit={(_, change, done) => save(change, done ?? "").then(() => null)} size="default" testId="task-sprint" />
          ) : (
            <span className="text-[13px]">{task.sprint ?? t("noSprint")}</span>
          )}
        </Field>
        <Field label={t("due")} htmlFor={writer ? `${ids}-due` : undefined}>
          {writer ? (
            <Input
              id={`${ids}-due`}
              type="date"
              defaultValue={task.due_on ?? ""}
              key={task.due_on ?? ""}
              onChange={(event) => {
                const due = event.target.value || null;
                if (due === task.due_on || (due !== null && !/^\d{4}-\d{2}-\d{2}$/.test(due))) return;
                void save({ due_on: due }, due ? t("dueSet", { ref: task.ref }) : t("dueCleared", { ref: task.ref }));
              }}
              data-testid="task-due-input"
            />
          ) : task.due_on ? (
            <DueDate task={task} />
          ) : (
            <span className="text-[13px] text-fg-subtle">{t("none")}</span>
          )}
        </Field>
        <ParentField key={task.parent ?? ""} project={project} task={task} writer={writer} save={save} />
        <PeopleField
          slot={t("assignees")}
          logins={task.assignees}
          people={people}
          writer={writer}
          onChange={(logins) => void save({ assignees: logins }, t("peopleSet", { ref: task.ref, slot: t("assignees") }))}
          testId="task-people-assignee"
        />
        {(["ba", "qa", "dev"] as const).map((role) => (
          <PeopleField
            key={role}
            slot={tRole(role)}
            logins={task.roles[role]}
            people={people}
            writer={writer}
            onChange={(logins) => void save({ roles: { [role]: logins } }, t("peopleSet", { ref: task.ref, slot: tRole(role) }))}
            testId={`task-people-${role}`}
          />
        ))}
        {writer ? <p className="text-xs text-fg-subtle">{t("peopleHint", { project })}</p> : null}
        {level ? (
          <Field label={t("visibility")}>
            <span className="text-[13px]">
              <VisibilityLevel level={level} />
            </span>
          </Field>
        ) : null}
      </CardContent>
    </Card>
  );
}
