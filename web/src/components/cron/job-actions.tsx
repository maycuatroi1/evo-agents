"use client";

import { CirclePause, CirclePlay, Ellipsis, History, type LucideIcon, PencilLine, Play, Trash2 } from "lucide-react";
import { useTranslations } from "next-intl";
import { Fragment, useId, useState } from "react";

import { ConfirmAction } from "@/components/feedback/confirm-action";
import { notify, notifyFailure } from "@/components/feedback/toast";
import { Button } from "@/components/ui/button";
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuSeparator, DropdownMenuTrigger } from "@/components/ui/dropdown-menu";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { browserApi } from "@/lib/api/browser";

import { useCronWrite } from "./hooks";
import { type CronJob, deleteCronJob, pauseCronJob, resumeCronJob, runCronJob } from "./queries";

export type CronAction = "history" | "edit" | "run" | "pause" | "resume" | "delete";
type Item = { id: CronAction; icon: LucideIcon; destructive?: boolean };

/** What the visitor may do to a job: the history for anyone; Edit for its owner; the rest for its owner or an admin. */
export function jobActions(job: CronJob): CronAction[] {
  const actions: CronAction[] = ["history"];
  if (job.can_change) actions.push("edit");
  if (job.can_manage) actions.push("run", job.paused ? "resume" : "pause", "delete");
  return actions;
}

const ICONS: Record<CronAction, LucideIcon> = { history: History, edit: PencilLine, run: Play, pause: CirclePause, resume: CirclePlay, delete: Trash2 };

/** A job's actions behind one icon button, at the end of its row. */
export function JobMenu({ job, onAction }: { job: CronJob; onAction: (action: CronAction) => void }) {
  const t = useTranslations("cron.actions");
  const items: Item[] = jobActions(job).map((id) => ({ id, icon: ICONS[id], destructive: id === "delete" }));
  return (
    <DropdownMenu modal={false}>
      <DropdownMenuTrigger asChild>
        <Button type="button" variant="ghost" size="icon-sm" aria-label={t("menu", { name: job.name })} data-testid="cron-job-menu">
          <Ellipsis aria-hidden="true" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="min-w-44">
        {items.map((item) => (
          <Fragment key={item.id}>
            {item.destructive ? <DropdownMenuSeparator /> : null}
            <DropdownMenuItem variant={item.destructive ? "destructive" : "default"} onSelect={() => onAction(item.id)} data-testid={`cron-action-${item.id}`}>
              <item.icon aria-hidden="true" />
              {t(item.id)}
            </DropdownMenuItem>
          </Fragment>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

/** Run now and Resume, which need no dialog: the hub's answer is a toast. */
export function useQuickActions(project: string) {
  const t = useTranslations("cron.actions");
  const { run } = useCronWrite<unknown>(project);
  const fail = (title: string) => (failure: Awaited<ReturnType<typeof run>>) => {
    if (failure) notifyFailure(title, failure);
  };
  return {
    runNow: (job: CronJob) =>
      void run(
        () => runCronJob(browserApi(), project, job.name),
        () => notify({ tone: "success", text: t("ranNow", { name: job.name }), description: t("ranNowText") }),
        { 403: t("forbidden"), 409: t("ranTwice") },
      ).then(fail(t("runFailed"))),
    resume: (job: CronJob) =>
      void run(
        () => resumeCronJob(browserApi(), project, job.name),
        () => notify({ tone: "success", text: t("resumed", { name: job.name }), description: t("resumedText") }),
        { 403: t("forbidden") },
      ).then(fail(t("resumeFailed"))),
  };
}

/** Pause a job, after a dialog that asks why; the reason, optional, shows in the list beside Paused. */
export function PauseDialog({ project, job, onClose }: { project: string; job: CronJob; onClose: () => void }) {
  const t = useTranslations("cron.actions");
  const id = useId();
  const [reason, setReason] = useState("");
  const { busy, refused, run } = useCronWrite<CronJob>(project);
  const confirm = () =>
    void run(
      () => pauseCronJob(browserApi(), project, job.name, reason.trim() || null),
      () => {
        notify({ tone: "success", text: t("paused", { name: job.name }) });
        onClose();
      },
      { 403: t("forbidden") },
    );
  return (
    <ConfirmAction
      open
      onOpenChange={(open) => (open ? undefined : onClose())}
      title={t("pauseTitle", { name: job.name })}
      description={
        <>
          <p>{t("pauseText")}</p>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor={id} className="text-foreground">
              {t("reason")}
            </Label>
            <Textarea id={id} value={reason} onChange={(event) => setReason(event.target.value)} maxLength={500} rows={3} aria-describedby={`${id}-hint`} data-testid="cron-pause-reason" />
            <p id={`${id}-hint`} className="text-xs text-muted-foreground">
              {t("reasonHint")}
            </p>
          </div>
        </>
      }
      confirmLabel={t("pause")}
      pendingLabel={t("pausing")}
      cancelLabel={t("cancel")}
      pending={busy}
      error={refused}
      onConfirm={confirm}
      testId="cron-pause-dialog"
    />
  );
}

/** Delete a job, after a dialog that says its fires go with it and its runs stay. */
export function DeleteDialog({ project, job, onClose }: { project: string; job: CronJob; onClose: () => void }) {
  const t = useTranslations("cron.actions");
  const { busy, refused, run } = useCronWrite<unknown>(project);
  const confirm = () =>
    void run(
      () => deleteCronJob(browserApi(), project, job.name),
      () => {
        notify({ tone: "success", text: t("deleted", { name: job.name }) });
        onClose();
      },
      { 403: t("forbidden"), 404: t("gone", { name: job.name }) },
    );
  return (
    <ConfirmAction
      open
      onOpenChange={(open) => (open ? undefined : onClose())}
      title={t("deleteTitle", { name: job.name })}
      description={<p>{t("deleteText", { owner: job.owner })}</p>}
      confirmLabel={t("delete")}
      pendingLabel={t("deleting")}
      cancelLabel={t("cancel")}
      pending={busy}
      error={refused}
      onConfirm={confirm}
      testId="cron-delete-dialog"
    />
  );
}
