"use client";

import { Send } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";
import { type FormEvent, useId, useState } from "react";

import { BrandMark } from "@/components/brand";
import { SafeMarkdown } from "@/components/data/markdown";
import { When } from "@/components/data/when";
import { InlineError, useWriteFailure, type WriteFailure } from "@/components/feedback/notice";
import { notify } from "@/components/feedback/toast";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { browserApi } from "@/lib/api/browser";

import { commentOn, type Priority, type TaskDetail, type TaskEvent, type TasksSettings } from "./api";
import { PersonAvatar } from "./badges";
import { signInAgainOn401, useTasksRefresh } from "./hooks";
import { eventWords, type NamedField, pullName, statusOf } from "./model";

const MAX_COMMENT = 20_000; // evo_agents.hub.tables_tasks.MAX_COMMENT_CHARS

/** A row of the history as a sentence, the project's names for statuses and the visitor's words for the rest. */
function useEventSentence(settings: TasksSettings) {
  const t = useTranslations("tasks.history");
  const tField = useTranslations("tasks.field");
  const tSlot = useTranslations("tasks.slot");
  const tLink = useTranslations("tasks.linkKind");
  const tPriority = useTranslations("tasks.priority");
  const format = useFormatter();
  const status = (key: string) => statusOf(settings, key)?.name ?? key;
  const value = (field: NamedField, raw: string) => {
    if (field === "priority" && ["urgent", "high", "medium", "low"].includes(raw)) return tPriority(raw as Priority);
    if (field === "due_on" && /^\d{4}-\d{2}-\d{2}$/.test(raw)) {
      return format.dateTime(new Date(`${raw}T00:00:00Z`), { dateStyle: "medium", timeZone: "UTC" });
    }
    return raw;
  };
  return (event: TaskEvent): string => {
    const words = eventWords(event);
    switch (words.kind) {
      case "created":
        return t("created");
      case "description":
        return t("description");
      case "status":
        return words.from ? t("status", { from: status(words.from), to: status(words.to) }) : t("statusSet", { to: status(words.to) });
      case "set":
        return words.from
          ? t("changed", { field: tField(words.field), from: value(words.field, words.from), to: value(words.field, words.to) })
          : t("set", { field: tField(words.field), to: value(words.field, words.to) });
      case "cleared":
        return t("cleared", { field: tField(words.field), from: value(words.field, words.from) });
      case "people":
        return words.to ? t("people", { slot: tSlot(words.field), people: words.to }) : t("peopleCleared", { slot: tSlot(words.field) });
      case "linked":
        return t("linked", { kind: tLink(words.link), target: words.link === "pull" ? pullName(words.target) : words.target });
      case "unlinked":
        return t("unlinked", { kind: tLink(words.link) });
      case "step":
        return t("step", { status: words.to ?? "" });
      default:
        return t("other", { field: words.field });
    }
  };
}

function CommentForm({ project, task }: { project: string; task: TaskDetail }) {
  const t = useTranslations("tasks.comments");
  const id = useId();
  const refresh = useTasksRefresh(project);
  const failure = useWriteFailure();
  const [body, setBody] = useState("");
  const [busy, setBusy] = useState(false);
  const [refused, setRefused] = useState<WriteFailure | null>(null);
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const text = body.trim();
    if (!text || busy) return;
    setBusy(true);
    setRefused(null);
    try {
      await commentOn(browserApi(), project, task.ref, text);
      setBody("");
      await refresh(task.ref);
      notify({ tone: "success", text: t("added") });
    } catch (error) {
      signInAgainOn401(error);
      setRefused(failure(error));
    } finally {
      setBusy(false);
    }
  };
  return (
    <form onSubmit={(event) => void submit(event)} className="flex flex-col gap-2 border-t pt-4" data-testid="task-comment-form">
      <Label htmlFor={id}>{t("label", { ref: task.ref })}</Label>
      <Textarea id={id} value={body} onChange={(event) => setBody(event.target.value)} rows={3} maxLength={MAX_COMMENT} data-testid="task-comment-body" />
      {refused ? <InlineError text={refused.text} detail={refused.detail} requestId={refused.requestId} /> : null}
      <Button type="submit" variant="secondary" size="sm" busy={busy} disabled={!body.trim()} className="self-end" data-testid="task-comment-submit">
        <Send aria-hidden="true" />
        {t("submit")}
      </Button>
    </form>
  );
}

/**
 * A task's history, oldest first: who changed what (or the hub, and why: a plan step turned done, an import's day),
 * then its comments, each in Markdown as people write it; a writer adds one.
 */
export function TaskActivity({ project, task, settings, writer }: { project: string; task: TaskDetail; settings: TasksSettings; writer: boolean }) {
  const t = useTranslations("tasks.history");
  const tComments = useTranslations("tasks.comments");
  const sentence = useEventSentence(settings);
  return (
    <>
      <Card size="sm" data-testid="task-history">
        <CardHeader>
          <CardTitle>
            <h2>{t("heading")}</h2>
          </CardTitle>
        </CardHeader>
        <CardContent>
          {task.events.length === 0 ? (
            <p className="text-[13px] text-fg-subtle">{t("none")}</p>
          ) : (
            <ol className="flex flex-col gap-3">
              {task.events.map((event, index) => (
                <li key={`${event.at}-${index}`} className="flex min-w-0 items-start gap-2.5 text-[13px]" data-testid="task-event" data-field={event.field}>
                  {event.actor ? <PersonAvatar login={event.actor} className="mt-px" /> : <BrandMark className="mt-px size-6 shrink-0" />}
                  <div className="flex min-w-0 flex-col gap-0.5">
                    <p className="[overflow-wrap:anywhere]">
                      <span className="font-medium text-foreground">{event.actor ?? t("hub")}</span> <span className="text-muted-foreground">{sentence(event)}</span>{" "}
                      <span className="text-xs text-fg-subtle">
                        <When value={event.at} />
                      </span>
                    </p>
                    {event.cause ? <p className="text-xs text-fg-subtle [overflow-wrap:anywhere]">{event.cause}</p> : null}
                  </div>
                </li>
              ))}
            </ol>
          )}
        </CardContent>
      </Card>
      <Card size="sm" data-testid="task-comments">
        <CardHeader>
          <CardTitle>
            <h2>{tComments("heading")}</h2>
          </CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          {task.comments.length === 0 ? <p className="text-[13px] text-fg-subtle">{tComments("none")}</p> : null}
          {task.comments.map((comment) => (
            <article key={comment.id} className="flex min-w-0 items-start gap-2.5" aria-label={tComments("by", { login: comment.author })} data-testid="task-comment">
              <PersonAvatar login={comment.author} className="mt-0.5" />
              <div className="flex min-w-0 flex-1 flex-col gap-1">
                <p className="flex flex-wrap items-baseline gap-x-2 text-xs text-muted-foreground">
                  <span className="text-[13px] font-medium text-foreground">{comment.author}</span>
                  <When value={comment.created_at} />
                </p>
                <SafeMarkdown testId="task-comment-text">{comment.body}</SafeMarkdown>
              </div>
            </article>
          ))}
          {writer ? <CommentForm project={project} task={task} /> : null}
        </CardContent>
      </Card>
    </>
  );
}
