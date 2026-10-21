"use client";

import { useQuery } from "@tanstack/react-query";
import { Lock, Plus } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import { type FormEvent, type ReactNode, useId, useState } from "react";

import { Banner } from "@/components/feedback/banner";
import { InlineError, useWriteFailure, type WriteFailure } from "@/components/feedback/notice";
import { notify } from "@/components/feedback/toast";
import { PageHeader } from "@/components/shell/page-header";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { PageSkeleton } from "@/components/states/states";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select";
import { Textarea } from "@/components/ui/textarea";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";

import { createTask, PRIORITIES, type Priority, type Sprint, sprintsQuery, tasksPeopleQuery, type TasksSettings, tasksSettingsQuery } from "./api";
import { TasksTabs } from "./chrome";
import { signInAgainOn401, useTasksRefresh, useTasksViewer } from "./hooks";
import { boardHref, taskHref } from "./links";
import { canWrite, linkCount, MAX_DESCRIPTION, MAX_TITLE, parseLinks, sprintChoices } from "./model";

type Form = { title: string; type: string; status: string; priority: Priority; sprint: string; due: string; assignee: string; description: string; links: string };

function Field({ id, label, hint, error, children }: { id: string; label: string; hint?: ReactNode; error?: string | null; children: ReactNode }) {
  return (
    <div className="flex min-w-0 flex-col gap-1.5">
      <Label htmlFor={id}>{label}</Label>
      {children}
      {error ? (
        <p id={`${id}-error`} className="text-xs font-medium text-danger">
          {error}
        </p>
      ) : hint ? (
        <p id={`${id}-hint`} className="text-xs text-fg-subtle">
          {hint}
        </p>
      ) : null}
    </div>
  );
}

function NewTaskForm({ project, settings, sprints, link, sprint }: { project: string; settings: TasksSettings; sprints: readonly Sprint[]; link: string | null; sprint: string | null }) {
  const t = useTranslations("tasks.newTask");
  const tPriority = useTranslations("tasks.priority");
  const ids = useId();
  const router = useRouter();
  const failure = useWriteFailure();
  const refresh = useTasksRefresh(project);
  const people = useQuery(tasksPeopleQuery(browserApi, project));
  const choices = sprintChoices(sprints, null);
  const [form, setForm] = useState<Form>({
    title: "",
    type: settings.types[0] ?? "task",
    status: settings.statuses[0]?.key ?? "",
    priority: "medium",
    sprint: sprint && choices.some((one) => one.name === sprint) ? sprint : "",
    due: "",
    assignee: "",
    description: "",
    links: link ?? "",
  });
  const [errors, setErrors] = useState<{ title?: string; links?: string }>({});
  const [busy, setBusy] = useState(false);
  const [refused, setRefused] = useState<WriteFailure | null>(null);
  const set = (patch: Partial<Form>) => setForm((held) => ({ ...held, ...patch }));
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (busy) return;
    const parsed = parseLinks(form.links);
    const found = {
      title: form.title.trim() ? undefined : t("titleMissing"),
      links: parsed.bad.length ? t("linksBad", { refs: parsed.bad.join(", ") }) : undefined,
    };
    setErrors(found);
    const first = found.title ? "title" : found.links ? "links" : null;
    if (first) {
      document.getElementById(`${ids}-${first}`)?.focus();
      return;
    }
    setBusy(true);
    setRefused(null);
    try {
      const made = await createTask(browserApi(), project, {
        title: form.title.trim(),
        description_md: form.description,
        type: form.type,
        status: form.status,
        priority: form.priority,
        sprint: form.sprint || null,
        due_on: form.due || null,
        assignees: form.assignee ? [form.assignee] : [],
        links: parsed.links,
      });
      await refresh();
      notify({ tone: "success", text: t("created", { ref: made.ref }), description: linkCount(parsed.links) ? t("createdLinks", { count: linkCount(parsed.links) }) : null });
      router.push(taskHref(project, made.ref));
    } catch (error) {
      signInAgainOn401(error);
      setRefused(failure(error));
      setBusy(false);
    }
  };
  const describe = (name: string, hint: boolean, error?: string) => (error ? `${ids}-${name}-error` : hint ? `${ids}-${name}-hint` : undefined);
  return (
    <form onSubmit={(event) => void submit(event)} noValidate className="flex max-w-3xl min-w-0 flex-col gap-5 rounded-md border bg-card p-4 md:p-6" aria-label={t("formLabel")} data-testid="task-new-form">
      <Field id={`${ids}-title`} label={t("title")} error={errors.title}>
        <Input
          id={`${ids}-title`}
          value={form.title}
          onChange={(event) => {
            set({ title: event.target.value });
            setErrors((held) => ({ ...held, title: undefined }));
          }}
          maxLength={MAX_TITLE}
          aria-required="true"
          aria-invalid={errors.title ? true : undefined}
          aria-describedby={describe("title", false, errors.title)}
          data-testid="task-new-title"
        />
      </Field>
      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
        <Field id={`${ids}-type`} label={t("type")}>
          <NativeSelect id={`${ids}-type`} value={form.type} onChange={(event) => set({ type: event.target.value })} className="w-full">
            {settings.types.map((type) => (
              <NativeSelectOption key={type} value={type}>
                {type}
              </NativeSelectOption>
            ))}
          </NativeSelect>
        </Field>
        <Field id={`${ids}-status`} label={t("status")}>
          <NativeSelect id={`${ids}-status`} value={form.status} onChange={(event) => set({ status: event.target.value })} className="w-full" data-testid="task-new-status">
            {settings.statuses.map((status) => (
              <NativeSelectOption key={status.key} value={status.key}>
                {status.name}
              </NativeSelectOption>
            ))}
          </NativeSelect>
        </Field>
        <Field id={`${ids}-priority`} label={t("priority")}>
          <NativeSelect id={`${ids}-priority`} value={form.priority} onChange={(event) => set({ priority: event.target.value as Priority })} className="w-full">
            {PRIORITIES.map((priority) => (
              <NativeSelectOption key={priority} value={priority}>
                {tPriority(priority)}
              </NativeSelectOption>
            ))}
          </NativeSelect>
        </Field>
        <Field id={`${ids}-sprint`} label={t("sprint")}>
          <NativeSelect id={`${ids}-sprint`} value={form.sprint} onChange={(event) => set({ sprint: event.target.value })} className="w-full" data-testid="task-new-sprint">
            <NativeSelectOption value="">{t("noSprint")}</NativeSelectOption>
            {choices.map((one) => (
              <NativeSelectOption key={one.name} value={one.name}>
                {one.name}
              </NativeSelectOption>
            ))}
          </NativeSelect>
        </Field>
        <Field id={`${ids}-due`} label={t("due")}>
          <Input id={`${ids}-due`} type="date" value={form.due} onChange={(event) => set({ due: event.target.value })} />
        </Field>
        <Field id={`${ids}-assignee`} label={t("assignee")}>
          <NativeSelect id={`${ids}-assignee`} value={form.assignee} onChange={(event) => set({ assignee: event.target.value })} className="w-full" data-testid="task-new-assignee">
            <NativeSelectOption value="">{t("nobody")}</NativeSelectOption>
            {(people.data ?? []).map((person) => (
              <NativeSelectOption key={person.login} value={person.login}>
                {person.login}
              </NativeSelectOption>
            ))}
          </NativeSelect>
        </Field>
      </div>
      <Field id={`${ids}-description`} label={t("description")} hint={t("descriptionHint")}>
        <Textarea
          id={`${ids}-description`}
          value={form.description}
          onChange={(event) => set({ description: event.target.value })}
          rows={8}
          maxLength={MAX_DESCRIPTION}
          aria-describedby={`${ids}-description-hint`}
          className="font-mono text-[13px] leading-5"
          data-testid="task-new-description"
        />
      </Field>
      <Field id={`${ids}-links`} label={t("links")} hint={settings.srs ? t("linksHint") : t("linksHintNoSrs")} error={errors.links}>
        <Input
          id={`${ids}-links`}
          value={form.links}
          onChange={(event) => {
            set({ links: event.target.value });
            setErrors((held) => ({ ...held, links: undefined }));
          }}
          autoComplete="off"
          spellCheck={false}
          aria-invalid={errors.links ? true : undefined}
          aria-describedby={describe("links", true, errors.links)}
          data-testid="task-new-links"
        />
      </Field>
      {refused ? <InlineError text={refused.text} detail={refused.detail} requestId={refused.requestId} /> : null}
      <div className="flex flex-wrap justify-end gap-2">
        <Button asChild variant="ghost">
          <Link href={boardHref(project)}>{t("cancel")}</Link>
        </Button>
        <Button type="submit" busy={busy} data-testid="task-new-create">
          <Plus aria-hidden="true" />
          {t("create")}
        </Button>
      </div>
    </form>
  );
}

/**
 * A new task (/p/{project}/x/tasks/board/new): its title, type, status, priority, sprint, due date, assignee,
 * description and links in one form. An SRS item's page opens it with `link` set to the item's code, the board with
 * `sprint` set to the sprint it shows. Creating a task takes the writer role.
 */
export function NewTaskPage({ project, title, link, sprint, initialError }: { project: string; title: string; link: string | null; sprint: string | null; initialError: ApiErrorInfo | null }) {
  const t = useTranslations("tasks.newTask");
  const viewer = useTasksViewer(project);
  const writer = canWrite(viewer);
  const settings = useHubQuery(tasksSettingsQuery(browserApi, project), initialError);
  const sprints = useHubQuery(sprintsQuery(browserApi, project));
  return (
    <div className="flex flex-col gap-6" data-testid="task-new-page">
      <div className="flex flex-col gap-4">
        <PageHeader title={title} sub={link ? t("linkedTo", { code: link }) : null} />
        <TasksTabs project={project} current={null} writer={writer} />
      </div>
      {viewer && !writer ? (
        <Banner icon={Lock} title={t("readerTitle", { project })} testId="task-new-reader">
          <Link href={boardHref(project)} className="text-brand underline underline-offset-4 hover:text-brand-hover">
            {t("backToBoard")}
          </Link>
        </Banner>
      ) : viewer ? (
        <QueryView state={settings} loading={<PageSkeleton />}>
          {(config) => (
            <QueryView state={sprints} loading={<PageSkeleton />}>
              {(known) => <NewTaskForm project={project} settings={config} sprints={known} link={link} sprint={sprint} />}
            </QueryView>
          )}
        </QueryView>
      ) : null}
    </div>
  );
}
