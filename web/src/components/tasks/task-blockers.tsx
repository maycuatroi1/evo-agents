"use client";

import { Lock, LockOpen } from "lucide-react";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { type FormEvent, useId, useState } from "react";

import { When } from "@/components/data/when";
import { InlineError, useWriteFailure, type WriteFailure } from "@/components/feedback/notice";
import { notify, notifyFailure } from "@/components/feedback/toast";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { NativeSelect, NativeSelectOption } from "@/components/ui/native-select";
import { browserApi } from "@/lib/api/browser";

import { addBlocker, editBlocker, type TaskBlocker, type TaskBlockerIn, type TaskDetail, type TaskPerson } from "./api";
import { signInAgainOn401, useTasksRefresh } from "./hooks";
import { decisionHref, taskHref } from "./links";

type WaitKind = TaskBlocker["waiting_kind"];

/** Who or what a blocker waits on: a member by login, another task as a link to its page, or the words given. */
function Waiting({ project, blocker }: { project: string; blocker: TaskBlocker }) {
  const t = useTranslations("tasks.blockers");
  if (blocker.waiting_kind === "task") {
    return (
      <span>
        {t("waitingTask")}{" "}
        <Link href={taskHref(project, blocker.waiting_on)} className="font-mono text-brand underline underline-offset-4 hover:text-brand-hover" data-testid="task-blocker-task">
          {blocker.waiting_on}
        </Link>
      </span>
    );
  }
  return (
    <span>
      {blocker.waiting_kind === "member" ? t("waitingMember") : t("waitingText")} <span className="font-medium [overflow-wrap:anywhere]">{blocker.waiting_on}</span>
    </span>
  );
}

function BlockerLine({ project, task, blocker, writer }: { project: string; task: TaskDetail; blocker: TaskBlocker; writer: boolean }) {
  const t = useTranslations("tasks.blockers");
  const refresh = useTasksRefresh(project);
  const failure = useWriteFailure();
  const [busy, setBusy] = useState(false);
  const clear = async () => {
    if (blocker.id === null) return;
    setBusy(true);
    try {
      await editBlocker(browserApi(), project, task.ref, blocker.id, { cleared: true });
      notify({ tone: "success", text: t("cleared", { ref: task.ref }) });
    } catch (error) {
      signInAgainOn401(error);
      notifyFailure(t("clearFailed", { ref: task.ref }), failure(error));
    } finally {
      await refresh(task.ref);
      setBusy(false);
    }
  };
  return (
    <li className="flex min-w-0 flex-col gap-1 rounded-md border bg-card p-3 text-[13px]" data-testid="task-blocker" data-kind={blocker.kind} data-blocker-id={blocker.id ?? undefined}>
      <div className="flex min-w-0 items-start gap-2">
        <p className="min-w-0 flex-1 font-medium text-pretty [overflow-wrap:anywhere]" data-testid="task-blocker-reason">
          {blocker.reason}
        </p>
        {writer && blocker.id !== null ? (
          <Button type="button" variant="ghost" size="sm" className="shrink-0 max-md:h-11" busy={busy} onClick={() => void clear()} aria-label={t("clear", { reason: blocker.reason })} data-testid="task-blocker-clear">
            <LockOpen aria-hidden="true" />
            {t("clearButton")}
          </Button>
        ) : null}
      </div>
      <p className="flex min-w-0 flex-wrap items-center gap-x-1.5 text-xs text-muted-foreground">
        <Waiting project={project} blocker={blocker} />
        <span aria-hidden="true">·</span>
        <span>
          {t("since")} <When value={blocker.since} />
        </span>
        {blocker.set_by ? (
          <>
            <span aria-hidden="true">·</span>
            <span>{t("setBy", { login: blocker.set_by })}</span>
          </>
        ) : null}
      </p>
      {blocker.kind === "decision" && blocker.decision_id !== null ? (
        <p className="text-xs">
          <Link href={decisionHref(blocker.decision_id)} className="text-brand underline underline-offset-4 hover:text-brand-hover" data-testid="task-blocker-decision">
            {t("openDecision", { id: blocker.decision_id })}
          </Link>{" "}
          <span className="text-fg-subtle">{t("decisionHint")}</span>
        </p>
      ) : null}
    </li>
  );
}

function AddBlocker({ project, task, people }: { project: string; task: TaskDetail; people: readonly TaskPerson[] }) {
  const t = useTranslations("tasks.blockers");
  const id = useId();
  const refresh = useTasksRefresh(project);
  const failure = useWriteFailure();
  const [reason, setReason] = useState("");
  const [kind, setKind] = useState<WaitKind>("text");
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);
  const [refused, setRefused] = useState<WriteFailure | null>(null);
  const body = (): TaskBlockerIn => {
    const waiting = value.trim();
    return {
      reason: reason.trim(),
      waiting_member: kind === "member" ? waiting : null,
      waiting_task: kind === "task" ? waiting : null,
      waiting_text: kind === "text" ? waiting : null,
    };
  };
  const ready = reason.trim() !== "" && value.trim() !== "";
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!ready || busy) return;
    setBusy(true);
    setRefused(null);
    try {
      await addBlocker(browserApi(), project, task.ref, body());
      setReason("");
      setValue("");
      notify({ tone: "success", text: t("added", { ref: task.ref }) });
    } catch (error) {
      signInAgainOn401(error);
      setRefused(failure(error));
    } finally {
      await refresh(task.ref);
      setBusy(false);
    }
  };
  return (
    <form onSubmit={(event) => void submit(event)} className="flex flex-col gap-3 border-t pt-4" data-testid="task-blocker-form">
      <div className="flex flex-col gap-1.5">
        <Label htmlFor={`${id}-reason`}>{t("reason")}</Label>
        <Input id={`${id}-reason`} value={reason} onChange={(event) => setReason(event.target.value)} maxLength={1000} autoComplete="off" placeholder={t("reasonPlaceholder")} data-testid="task-blocker-reason-input" />
      </div>
      <div className="grid gap-3 sm:grid-cols-[10rem_minmax(0,1fr)]">
        <div className="flex flex-col gap-1.5">
          <Label htmlFor={`${id}-kind`}>{t("waitsOn")}</Label>
          <NativeSelect
            id={`${id}-kind`}
            value={kind}
            onChange={(event) => {
              setKind(event.target.value as WaitKind);
              setValue("");
            }}
            data-testid="task-blocker-kind"
          >
            <NativeSelectOption value="text">{t("kindText")}</NativeSelectOption>
            <NativeSelectOption value="member">{t("kindMember")}</NativeSelectOption>
            <NativeSelectOption value="task">{t("kindTask")}</NativeSelectOption>
          </NativeSelect>
        </div>
        <div className="flex min-w-0 flex-col gap-1.5">
          <Label htmlFor={`${id}-value`}>{kind === "member" ? t("kindMember") : kind === "task" ? t("kindTask") : t("kindText")}</Label>
          {kind === "member" ? (
            <NativeSelect id={`${id}-value`} value={value} onChange={(event) => setValue(event.target.value)} data-testid="task-blocker-value">
              <NativeSelectOption value="">{t("memberPlaceholder")}</NativeSelectOption>
              {people.map((person) => (
                <NativeSelectOption key={person.login} value={person.login}>
                  {person.login}
                </NativeSelectOption>
              ))}
            </NativeSelect>
          ) : (
            <Input
              id={`${id}-value`}
              value={value}
              onChange={(event) => setValue(event.target.value)}
              maxLength={kind === "text" ? 300 : 20}
              autoComplete="off"
              spellCheck={false}
              placeholder={kind === "task" ? t("taskPlaceholder") : t("textPlaceholder")}
              data-testid="task-blocker-value"
            />
          )}
        </div>
      </div>
      <div>
        <Button type="submit" variant="secondary" busy={busy} disabled={!ready} data-testid="task-blocker-add">
          <Lock aria-hidden="true" />
          {t("addButton")}
        </Button>
      </div>
      {refused ? <InlineError text={refused.text} detail={refused.detail} requestId={refused.requestId} /> : null}
    </form>
  );
}

/**
 * What a task waits on: every open blocker with who or what it waits on, why and since when. A blocker from an open
 * decision links to the decision and ends when it is answered; a writer clears the ones set by hand and adds one.
 */
export function TaskBlockers({ project, task, people, writer }: { project: string; task: TaskDetail; people: readonly TaskPerson[]; writer: boolean }) {
  const t = useTranslations("tasks.blockers");
  const titleId = useId();
  return (
    <Card data-testid="task-blockers" data-blocked={task.blockers.length > 0 || undefined}>
      <CardHeader>
        <CardTitle id={titleId} className="flex items-center gap-2 text-base">
          <Lock className="size-4 text-muted-foreground" aria-hidden="true" />
          {t("heading")}
        </CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {task.blockers.length === 0 ? (
          <p className="text-[13px] text-fg-subtle" data-testid="task-blockers-empty">
            {t("none")}
          </p>
        ) : (
          <ul className="flex flex-col gap-2" role="list" aria-labelledby={titleId}>
            {task.blockers.map((blocker) => (
              <BlockerLine key={`${blocker.kind}-${blocker.id ?? blocker.decision_id}`} project={project} task={task} blocker={blocker} writer={writer} />
            ))}
          </ul>
        )}
        {writer ? <AddBlocker project={project} task={task} people={people} /> : null}
      </CardContent>
    </Card>
  );
}
