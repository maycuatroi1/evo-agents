"use client";

import { Activity, EyeOff, GitPullRequest, Link2, ListChecks, type LucideIcon, Route as RouteIcon, X } from "lucide-react";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { type FormEvent, type ReactNode, useId, useState } from "react";

import { Identifier, NAME_LINK, RunRef, Tag } from "@/components/data/identifier";
import { InlineError, useWriteFailure, type WriteFailure } from "@/components/feedback/notice";
import { notify, notifyFailure } from "@/components/feedback/toast";
import { OtherStatusBadge, type RunStatus, STATUS_LOOKS, StatusBadge, type StepStatus } from "@/components/status/status-badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { srsItemHref } from "@/lib/core-plugin-links";

import type { TaskDetail, TaskLink, TasksSettings } from "./api";
import { signInAgainOn401, useTaskSave } from "./hooks";
import { planStepHref, runHref } from "./links";
import { isItemKind, linkCount, parseLinks, pullName, statusOf } from "./model";

const STEP_STATES: ReadonlySet<string> = new Set<StepStatus>(["pending", "in_progress", "blocked", "done"]);
const RUN_STATES: ReadonlySet<string> = new Set(Object.keys(STATUS_LOOKS.run));

function RunState({ state }: { state: string | null }) {
  if (!state) return null;
  return RUN_STATES.has(state) ? <StatusBadge kind="run" status={state as RunStatus} /> : <OtherStatusBadge raw={state} />;
}

function Group({ icon: Icon, title, empty, count, children, testId }: { icon: LucideIcon; title: string; empty: string; count: number; children: ReactNode; testId: string }) {
  return (
    <div className="flex flex-col gap-2" data-testid={testId}>
      <h3 className="flex items-center gap-1.5 text-xs font-medium text-muted-foreground">
        <Icon className="size-3.5" aria-hidden="true" />
        {title}
      </h3>
      {count === 0 ? (
        <p className="text-[13px] text-fg-subtle">{empty}</p>
      ) : (
        <ul className="flex flex-col gap-1.5" role="list" aria-label={title}>
          {children}
        </ul>
      )}
    </div>
  );
}

/** One link as a line: what it points at now, then its remove button for a writer. */
function Line({ link, name, onRemove, busy, children }: { link: TaskLink; name: string; onRemove: ((link: TaskLink, name: string) => void) | null; busy: boolean; children: ReactNode }) {
  const t = useTranslations("tasks.links");
  return (
    <li className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 text-[13px]" data-testid="task-link" data-kind={link.kind} data-link-id={link.id}>
      {link.hidden ? (
        <span className="inline-flex items-center gap-1.5 text-muted-foreground">
          <EyeOff className="size-3.5" aria-hidden="true" />
          {t("hidden")}
        </span>
      ) : (
        children
      )}
      {onRemove ? (
        <Button
          type="button"
          variant="ghost"
          size="icon-sm"
          className="ml-auto"
          aria-label={t("remove", { name })}
          disabled={busy}
          onClick={() => onRemove(link, name)}
          data-testid="task-link-remove"
        >
          <X aria-hidden="true" />
        </Button>
      ) : null}
    </li>
  );
}

function AddLink({ project, task }: { project: string; task: TaskDetail }) {
  const t = useTranslations("tasks.links");
  const id = useId();
  const save = useTaskSave(project);
  const failure = useWriteFailure();
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [refused, setRefused] = useState<WriteFailure | null>(null);
  const [bad, setBad] = useState<string[]>([]);
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    const { links, bad: wrong } = parseLinks(text);
    setBad(wrong);
    if (wrong.length || linkCount(links) === 0 || busy) return;
    setBusy(true);
    setRefused(null);
    try {
      await save(task.ref, { links });
      setText("");
      notify({ tone: "success", text: t("added", { count: linkCount(links), ref: task.ref }) });
    } catch (error) {
      signInAgainOn401(error);
      setRefused(failure(error));
    } finally {
      setBusy(false);
    }
  };
  return (
    <form onSubmit={(event) => void submit(event)} className="flex flex-col gap-2 border-t pt-4" data-testid="task-link-form">
      <Label htmlFor={id}>{t("add")}</Label>
      <div className="flex flex-col gap-2 sm:flex-row">
        <Input
          id={id}
          value={text}
          onChange={(event) => {
            setText(event.target.value);
            setBad([]);
          }}
          placeholder={t("placeholder")}
          aria-describedby={`${id}-hint`}
          aria-invalid={bad.length ? true : undefined}
          autoComplete="off"
          spellCheck={false}
          className="min-w-0 flex-1"
          data-testid="task-link-input"
        />
        <Button type="submit" variant="secondary" busy={busy} disabled={!text.trim()} data-testid="task-link-add">
          <Link2 aria-hidden="true" />
          {t("addButton")}
        </Button>
      </div>
      <p id={`${id}-hint`} className={bad.length ? "text-xs font-medium text-danger" : "text-xs text-fg-subtle"}>
        {bad.length ? t("bad", { refs: bad.join(", ") }) : t("hint")}
      </p>
      {refused ? <InlineError text={refused.text} detail={refused.detail} requestId={refused.requestId} /> : null}
    </form>
  );
}

/**
 * What a task links to, as it is now: the SRS items it implements (each opening its page while the project enables
 * srs, Retired once dropped from its document), the plan steps with their status in the plan and latest runs, runs and
 * pull requests. A link to what the visitor may not read shows that one is there and nothing of it. A writer removes a
 * link, and adds codes, plan steps, runs and pull requests in one line.
 */
export function TaskLinks({ project, task, settings, writer }: { project: string; task: TaskDetail; settings: TasksSettings; writer: boolean }) {
  const t = useTranslations("tasks.links");
  const tKind = useTranslations("tasks.itemKind");
  const save = useTaskSave(project);
  const failure = useWriteFailure();
  const [removing, setRemoving] = useState<number | null>(null);
  const of = (kind: TaskLink["kind"]) => task.links.filter((link) => link.kind === kind);
  const remove = async (link: TaskLink, name: string) => {
    setRemoving(link.id);
    try {
      await save(task.ref, { unlink: [link.id] });
      notify({ tone: "success", text: t("removed", { name, ref: task.ref }) });
    } catch (error) {
      signInAgainOn401(error);
      notifyFailure(t("removeFailed", { name }), failure(error));
    } finally {
      setRemoving(null);
    }
  };
  const onRemove = writer ? (link: TaskLink, name: string) => void remove(link, name) : null;
  const srs = of("srs_item");
  const steps = of("plan_step");
  const runs = of("run");
  const pulls = of("pull");
  const doneStatus = settings.on_steps_done ? (statusOf(settings, settings.on_steps_done)?.name ?? settings.on_steps_done) : null;
  return (
    <Card size="sm" data-testid="task-links">
      <CardHeader>
        <CardTitle>
          <h2>{t("heading")}</h2>
        </CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-5">
        {settings.srs || srs.length ? (
          <Group icon={RouteIcon} title={t("srs")} empty={t("noSrs")} count={srs.length} testId="task-links-srs">
            {srs.map((link) => (
              <Line key={link.id} link={link} name={link.code ?? t("item")} onRemove={onRemove} busy={removing === link.id}>
                {link.code ? (
                  <Identifier value={link.code} href={settings.srs && link.document ? srsItemHref(project, link.document, link.code) : undefined} data-code={link.code} />
                ) : null}
                <span className="min-w-0 [overflow-wrap:anywhere]">{link.item_title}</span>
                {link.item_kind ? <Tag>{isItemKind(link.item_kind) ? tKind(link.item_kind) : link.item_kind}</Tag> : null}
                {link.retired ? <Tag className="bg-attention-soft text-attention">{t("retired")}</Tag> : null}
              </Line>
            ))}
          </Group>
        ) : null}
        <Group icon={ListChecks} title={t("steps")} empty={t("noSteps")} count={steps.length} testId="task-links-steps">
          {steps.map((link) => {
            const name = `${link.plan_id}#${link.step}`;
            return (
              <Line key={link.id} link={link} name={name} onRemove={onRemove} busy={removing === link.id}>
                {link.plan_id && link.step ? (
                  <Link href={planStepHref(project, link.plan_id, link.step)} className={`${NAME_LINK} font-mono text-[13px] [overflow-wrap:anywhere]`}>
                    {name}
                  </Link>
                ) : null}
                <span className="min-w-0 text-muted-foreground [overflow-wrap:anywhere]">{link.step_title ?? t("noStep")}</span>
                {link.step_status && STEP_STATES.has(link.step_status) ? (
                  <StatusBadge kind="step" status={link.step_status as StepStatus} />
                ) : link.step_status ? (
                  <OtherStatusBadge raw={link.step_status} />
                ) : null}
                {link.runs.map((run) => (
                  <RunRef key={run.id} id={run.id} href={runHref(project, run.id)} label={t("openRun", { id: run.id, state: run.state })} />
                ))}
              </Line>
            );
          })}
        </Group>
        {steps.length && doneStatus ? <p className="-mt-3 text-xs text-fg-subtle">{t("onStepsDone", { status: doneStatus })}</p> : null}
        <Group icon={Activity} title={t("runs")} empty={t("noRuns")} count={runs.length} testId="task-links-runs">
          {runs.map((link) => (
            <Line key={link.id} link={link} name={`#${link.run_id}`} onRemove={onRemove} busy={removing === link.id}>
              {link.run_id ? <RunRef id={link.run_id} href={runHref(project, link.run_id)} label={t("openRun", { id: link.run_id, state: link.run_state ?? "" })} /> : null}
              {link.run_kind ? <Tag>{link.run_kind}</Tag> : null}
              <RunState state={link.run_state} />
            </Line>
          ))}
        </Group>
        <Group icon={GitPullRequest} title={t("pulls")} empty={t("noPulls")} count={pulls.length} testId="task-links-pulls">
          {pulls.map((link) => (
            <Line key={link.id} link={link} name={link.url ? pullName(link.url) : t("pull")} onRemove={onRemove} busy={removing === link.id}>
              {link.url ? <Identifier value={link.url} externalHref={link.url} linkLabel={t("opensForge", { name: pullName(link.url) })}>{pullName(link.url)}</Identifier> : null}
            </Line>
          ))}
        </Group>
        {writer ? <AddLink project={project} task={task} /> : null}
      </CardContent>
    </Card>
  );
}
