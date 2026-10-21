"use client";

import { Pencil } from "lucide-react";
import { useTranslations } from "next-intl";
import { type FormEvent, useId, useState } from "react";

import { SafeMarkdown } from "@/components/data/markdown";
import { InlineError, useWriteFailure, type WriteFailure } from "@/components/feedback/notice";
import { notify } from "@/components/feedback/toast";
import { Button } from "@/components/ui/button";
import { Card, CardAction, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";

import type { TaskDetail } from "./api";
import { signInAgainOn401, useTaskSave } from "./hooks";
import { MAX_DESCRIPTION, MAX_TITLE } from "./model";

function Editor({ project, task, onDone }: { project: string; task: TaskDetail; onDone: () => void }) {
  const t = useTranslations("tasks.description");
  const ids = useId();
  const save = useTaskSave(project);
  const failure = useWriteFailure();
  const [title, setTitle] = useState(task.title);
  const [body, setBody] = useState(task.description_md);
  const [busy, setBusy] = useState(false);
  const [refused, setRefused] = useState<WriteFailure | null>(null);
  const [missing, setMissing] = useState(false);
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (busy) return;
    if (!title.trim()) {
      setMissing(true);
      document.getElementById(`${ids}-title`)?.focus();
      return;
    }
    const change = {
      ...(title.trim() !== task.title ? { title: title.trim() } : {}),
      ...(body !== task.description_md ? { description_md: body } : {}),
    };
    if (Object.keys(change).length === 0) {
      onDone();
      return;
    }
    setBusy(true);
    setRefused(null);
    try {
      await save(task.ref, change);
      notify({ tone: "success", text: t("saved", { ref: task.ref }) });
      onDone();
    } catch (error) {
      signInAgainOn401(error);
      setRefused(failure(error));
    } finally {
      setBusy(false);
    }
  };
  return (
    <form onSubmit={(event) => void submit(event)} noValidate className="flex flex-col gap-4" data-testid="task-editor">
      <div className="flex flex-col gap-1.5">
        <Label htmlFor={`${ids}-title`}>{t("title")}</Label>
        <Input
          id={`${ids}-title`}
          value={title}
          onChange={(event) => {
            setTitle(event.target.value);
            setMissing(false);
          }}
          maxLength={MAX_TITLE}
          aria-invalid={missing || undefined}
          aria-describedby={missing ? `${ids}-title-error` : undefined}
          data-testid="task-editor-title"
        />
        {missing ? (
          <p id={`${ids}-title-error`} className="text-xs font-medium text-danger">
            {t("titleMissing")}
          </p>
        ) : null}
      </div>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor={`${ids}-body`}>{t("body")}</Label>
        <Textarea
          id={`${ids}-body`}
          value={body}
          onChange={(event) => setBody(event.target.value)}
          rows={10}
          maxLength={MAX_DESCRIPTION}
          aria-describedby={`${ids}-body-hint`}
          className="font-mono text-[13px] leading-5"
          data-testid="task-editor-body"
        />
        <p id={`${ids}-body-hint`} className="text-xs text-fg-subtle">
          {t("bodyHint")}
        </p>
      </div>
      {refused ? <InlineError text={refused.text} detail={refused.detail} requestId={refused.requestId} /> : null}
      <div className="flex flex-wrap justify-end gap-2">
        <Button type="button" variant="ghost" onClick={onDone} disabled={busy}>
          {t("cancel")}
        </Button>
        <Button type="submit" busy={busy} data-testid="task-editor-save">
          {t("save")}
        </Button>
      </div>
    </form>
  );
}

/** A task's description in Markdown, read as the hub renders people's text; a writer edits it with the title. */
export function TaskDescription({ project, task, writer }: { project: string; task: TaskDetail; writer: boolean }) {
  const t = useTranslations("tasks.description");
  const [editing, setEditing] = useState(false);
  return (
    <Card size="sm" data-testid="task-description">
      <CardHeader>
        <CardTitle>
          <h2>{t("heading")}</h2>
        </CardTitle>
        {writer && !editing ? (
          <CardAction>
            <Button type="button" variant="ghost" size="sm" onClick={() => setEditing(true)} data-testid="task-edit">
              <Pencil aria-hidden="true" />
              {t("edit")}
            </Button>
          </CardAction>
        ) : null}
      </CardHeader>
      <CardContent>
        {editing ? (
          <Editor project={project} task={task} onDone={() => setEditing(false)} />
        ) : task.description_md.trim() ? (
          <SafeMarkdown testId="task-description-body">{task.description_md}</SafeMarkdown>
        ) : (
          <p className="text-[13px] text-fg-subtle">{t("none")}</p>
        )}
      </CardContent>
    </Card>
  );
}
