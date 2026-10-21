"use client";

import { useQuery } from "@tanstack/react-query";
import { ArrowRight, Bug, CircleCheck, Power, Rocket, Undo2 } from "lucide-react";
import type { Route } from "next";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { type FormEvent, useId, useState } from "react";

import { ConfirmAction } from "@/components/feedback/confirm-action";
import { useWriteFailure } from "@/components/feedback/notice";
import { notify, notifyFailure } from "@/components/feedback/toast";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { browserApi } from "@/lib/api/browser";
import { MAX_SAMPLE_NOTE_CHARS, mergeQueueHref, mergeQueueQuery, type MergeQueueView, REVERTABLE_STATES } from "@/lib/merge-queue";
import { useAnswerSample, useApproveRelease, useAskRevert, useResumeRepo } from "@/lib/use-merge-actions";

import { mergeAsk } from "./model";
import type { Notification } from "./queries";

const number = (value: unknown): number | null => (typeof value === "number" && Number.isInteger(value) && value > 0 ? value : null);

function suspectsOf(details: Notification["details"]): { queueId: number; iid: number | null }[] {
  const listed = Array.isArray(details?.suspects) ? details.suspects : [];
  return listed.flatMap((item) => {
    const entry = item && typeof item === "object" ? (item as Record<string, unknown>) : {};
    const queueId = number(entry.queue_id);
    return queueId === null ? [] : [{ queueId, iid: number(entry.iid) }];
  });
}

const STATUS = "text-xs text-muted-foreground";

function SampleActions({ project, sampleId, view, done }: { project: string; sampleId: number; view: MergeQueueView | undefined; done: () => void }) {
  const t = useTranslations("inbox.merge");
  const ids = useId();
  const write = useAnswerSample(project);
  const failure = useWriteFailure();
  const [faulty, setFaulty] = useState(false);
  const [note, setNote] = useState("");
  const sample = view?.samples.find((item) => item.id === sampleId);
  if (sample && sample.result !== null) {
    return <p className={STATUS} data-testid="merge-notice-status">{t("sampleAnswered", { result: sample.result, login: sample.answered_by ?? "?" })}</p>;
  }
  const answer = async (result: "ok" | "faulty") => {
    try {
      await write.mutateAsync({ sampleId, result, note: result === "faulty" ? note.trim() || null : null });
      done();
      notify({ tone: "success", text: result === "ok" ? t("sampleOk") : t("sampleFaulty") });
    } catch (error) {
      notifyFailure(t("sampleFailed"), failure(error, { 409: t("sampleConflict"), 403: t("forbidden") }));
    }
  };
  if (faulty) {
    const submit = (event: FormEvent) => {
      event.preventDefault();
      if (!write.isPending) void answer("faulty");
    };
    return (
      <form onSubmit={submit} className="flex w-full flex-col gap-2" data-testid="merge-notice-faulty">
        <Label htmlFor={`${ids}-note`} className="text-xs">
          {t("faultyNote")}
        </Label>
        <Textarea id={`${ids}-note`} rows={2} value={note} maxLength={MAX_SAMPLE_NOTE_CHARS} onChange={(event) => setNote(event.target.value)} data-testid="merge-notice-note" />
        <div className="flex flex-wrap gap-2">
          <Button type="submit" size="sm" busy={write.isPending} data-testid="merge-notice-faulty-send">
            <Bug aria-hidden="true" />
            {t("faultySend")}
          </Button>
          <Button type="button" size="sm" variant="ghost" onClick={() => setFaulty(false)} disabled={write.isPending}>
            {t("cancel")}
          </Button>
        </div>
      </form>
    );
  }
  return (
    <>
      <Button type="button" size="sm" variant="secondary" busy={write.isPending} onClick={() => void answer("ok")} data-testid="merge-notice-ok">
        <CircleCheck aria-hidden="true" />
        {t("ok")}
      </Button>
      <Button type="button" size="sm" variant="quiet-danger" onClick={() => setFaulty(true)} data-testid="merge-notice-faulty-open">
        <Bug aria-hidden="true" />
        {t("faulty")}
      </Button>
    </>
  );
}

function ReleaseAction({ project, queueId, view, done }: { project: string; queueId: number; view: MergeQueueView | undefined; done: () => void }) {
  const t = useTranslations("inbox.merge");
  const write = useApproveRelease(project);
  const failure = useWriteFailure();
  const row = view?.requests.find((item) => item.id === queueId);
  if (row && !(row.state === "releasing" && row.uat_result === "passed")) {
    return <p className={STATUS} data-testid="merge-notice-status">{t("releaseGone", { state: row.state })}</p>;
  }
  const approve = async () => {
    try {
      await write.mutateAsync(queueId);
      done();
      notify({ tone: "success", text: t("released"), description: t("releasedText") });
    } catch (error) {
      notifyFailure(t("releaseFailed"), failure(error, { 409: t("releaseConflict"), 403: t("forbidden") }));
    }
  };
  return (
    <Button type="button" size="sm" busy={write.isPending} onClick={() => void approve()} data-testid="merge-notice-release">
      <Rocket aria-hidden="true" />
      {t("release")}
    </Button>
  );
}

/** A confirmed write of the notice: the revert of a suspect, or the reset of a breaker. */
function Confirmed({
  label,
  icon: Icon,
  title,
  description,
  confirm,
  run,
  conflict,
  done,
  testId,
}: {
  label: string;
  icon: typeof Power;
  title: string;
  description: string;
  confirm: string;
  run: () => Promise<unknown>;
  conflict: string;
  done: (text: string) => void;
  testId: string;
}) {
  const t = useTranslations("inbox.merge");
  const failure = useWriteFailure();
  const [open, setOpen] = useState(false);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<ReturnType<typeof failure> | null>(null);
  const go = async () => {
    setPending(true);
    try {
      await run();
      setOpen(false);
      done(label);
    } catch (failed) {
      setError(failure(failed, { 409: conflict, 403: t("forbidden") }));
    } finally {
      setPending(false);
    }
  };
  return (
    <>
      <Button
        type="button"
        size="sm"
        variant="secondary"
        onClick={() => {
          setError(null);
          setOpen(true);
        }}
        data-testid={testId}
      >
        <Icon aria-hidden="true" />
        {label}
      </Button>
      <ConfirmAction
        open={open}
        onOpenChange={setOpen}
        title={title}
        description={<p>{description}</p>}
        confirmLabel={confirm}
        pendingLabel={t("pending")}
        cancelLabel={t("cancel")}
        pending={pending}
        error={error}
        onConfirm={() => void go()}
        testId={`${testId}-dialog`}
      />
    </>
  );
}

function RevertActions({ project, notification, view, done }: { project: string; notification: Notification; view: MergeQueueView | undefined; done: () => void }) {
  const t = useTranslations("inbox.merge");
  const write = useAskRevert(project);
  const suspects = suspectsOf(notification.details).filter(({ queueId }) => {
    const row = view?.requests.find((item) => item.id === queueId);
    return !row || (row.source !== "revert" && (REVERTABLE_STATES as readonly string[]).includes(row.state));
  });
  if (suspects.length === 0) return <p className={STATUS} data-testid="merge-notice-status">{t("revertGone")}</p>;
  return (
    <>
      {suspects.map(({ queueId, iid }) => {
        const name = iid !== null ? `!${iid}` : `#${queueId}`;
        return (
          <Confirmed
            key={queueId}
            label={t("revert", { name })}
            icon={Undo2}
            title={t("revertTitle", { name })}
            description={t("revertText")}
            confirm={t("revertConfirm")}
            run={() => write.mutateAsync(queueId)}
            conflict={t("revertConflict")}
            done={() => {
              done();
              notify({ tone: "success", text: t("reverted", { name }), description: t("revertedText") });
            }}
            testId="merge-notice-revert"
          />
        );
      })}
    </>
  );
}

function ResumeAction({ project, repo, view, done }: { project: string; repo: string; view: MergeQueueView | undefined; done: () => void }) {
  const t = useTranslations("inbox.merge");
  const write = useResumeRepo(project);
  const breaker = view?.breakers.find((item) => item.repo === repo);
  if (breaker && breaker.state !== "tripped") {
    return <p className={STATUS} data-testid="merge-notice-status">{t("resumeGone", { login: breaker.resumed_by ?? "?" })}</p>;
  }
  return (
    <Confirmed
      label={t("resume")}
      icon={Power}
      title={t("resumeTitle", { repo })}
      description={t("resumeText")}
      confirm={t("resumeConfirm")}
      run={() => write.mutateAsync(repo)}
      conflict={t("resumeConflict")}
      done={() => {
        done();
        notify({ tone: "success", text: t("resumed", { repo }) });
      }}
      testId="merge-notice-resume"
    />
  );
}

/**
 * The answer a notice of the merge queue asks of an admin, in the notice itself: the merge queue page's link, and
 * OK or Faulty, Approve release, Revert one of the suspects or Reset breaker. The project's queue tells whether the
 * question is still open; once it is answered, here or elsewhere, the notice says so instead of offering the buttons.
 */
export function MergeNoticeActions({ notification, onRead }: { notification: Notification; onRead: (id: number) => void }) {
  const t = useTranslations("inbox.merge");
  const ask = mergeAsk(notification);
  const project = notification.project ?? "";
  const queue = useQuery({ ...mergeQueueQuery(browserApi, project), enabled: ask !== null });
  if (ask === null) return null;
  const details = notification.details ?? {};
  const done = () => {
    if (notification.read_at === null) onRead(notification.id);
  };
  const link = typeof notification.link === "string" && notification.link.startsWith("/") && !notification.link.startsWith("//") ? (notification.link as Route) : mergeQueueHref(project);
  const sampleId = number(details.sample_id);
  const queueId = number(details.queue_id);
  return (
    <div className="flex w-full flex-wrap items-center gap-2" data-testid="merge-notice" data-ask={ask}>
      {ask === "sample" && sampleId !== null ? <SampleActions project={project} sampleId={sampleId} view={queue.data} done={done} /> : null}
      {ask === "release" && queueId !== null ? <ReleaseAction project={project} queueId={queueId} view={queue.data} done={done} /> : null}
      {ask === "revert" ? <RevertActions project={project} notification={notification} view={queue.data} done={done} /> : null}
      {ask === "resume" ? <ResumeAction project={project} repo={String(details.repo)} view={queue.data} done={done} /> : null}
      <Link href={link} className="inline-flex min-h-7 items-center gap-1 text-sm font-medium text-brand underline-offset-4 hover:underline max-md:min-h-11" data-testid="merge-notice-open">
        {t("open")}
        <ArrowRight className="size-3.5" aria-hidden="true" />
      </Link>
    </div>
  );
}
