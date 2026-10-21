"use client";

import { Bug, CircleCheck, ExternalLink } from "lucide-react";
import { useTranslations } from "next-intl";
import { type FormEvent, useId, useState } from "react";

import { When } from "@/components/data/when";
import { InlineError, useWriteFailure } from "@/components/feedback/notice";
import { notify, notifyFailure } from "@/components/feedback/toast";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { MAX_SAMPLE_NOTE_CHARS, type MergeSample } from "@/lib/merge-queue";
import { useAnswerSample } from "@/lib/use-merge-actions";

import { SampleBadge } from "./badges";
import { Panel, TEXT_LINK } from "./chrome";
import { forgeUrl } from "./model";

function sampleName(sample: Pick<MergeSample, "iid" | "repo" | "queue_id">): string {
  return sample.iid !== null ? `!${sample.iid} ${sample.repo}` : `#${sample.queue_id} ${sample.repo}`;
}

/** Faulty, with the admin's note: a dialog, since it counts against the Judge and may trip the repo's breaker. */
function FaultyDialog({ project, sample, open, onOpenChange }: { project: string; sample: MergeSample; open: boolean; onOpenChange: (open: boolean) => void }) {
  const t = useTranslations("mergeQueue.sample");
  const ids = useId();
  const [note, setNote] = useState("");
  const write = useAnswerSample(project);
  const failure = useWriteFailure();
  const error = write.isError ? failure(write.error, { 409: t("answered"), 403: t("forbidden") }) : null;
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (write.isPending) return;
    try {
      await write.mutateAsync({ sampleId: sample.id, result: "faulty", note: note.trim() || null });
      onOpenChange(false);
      notify({ tone: "success", text: t("faultyDone", { name: sampleName(sample) }), description: t("faultyDoneText") });
    } catch {
      // The reason shows above the buttons.
    }
  };
  return (
    <Dialog open={open} onOpenChange={(next) => (write.isPending ? undefined : onOpenChange(next))}>
      <DialogContent className="sm:max-w-lg" data-testid="sample-faulty-dialog">
        <form onSubmit={(event) => void submit(event)} className="flex flex-col gap-4" noValidate>
          <DialogHeader>
            <DialogTitle>{t("faultyTitle", { name: sampleName(sample) })}</DialogTitle>
            <DialogDescription>{t("faultyDescription")}</DialogDescription>
          </DialogHeader>
          <div className="flex flex-col gap-1.5">
            <Label htmlFor={`${ids}-note`}>{t("note")}</Label>
            <Textarea
              id={`${ids}-note`}
              value={note}
              rows={4}
              maxLength={MAX_SAMPLE_NOTE_CHARS}
              onChange={(event) => setNote(event.target.value)}
              aria-describedby={`${ids}-note-hint`}
              data-testid="sample-note"
            />
            <p id={`${ids}-note-hint`} className="text-xs text-fg-subtle">
              {t("noteHint", { max: MAX_SAMPLE_NOTE_CHARS })}
            </p>
          </div>
          {error ? <InlineError text={error.text} detail={error.detail} requestId={error.requestId} /> : null}
          <DialogFooter>
            <Button type="button" variant="outline" size="lg" onClick={() => onOpenChange(false)} disabled={write.isPending}>
              {t("cancel")}
            </Button>
            <Button type="submit" size="lg" busy={write.isPending} data-testid="sample-faulty-confirm">
              <Bug aria-hidden="true" />
              {write.isPending ? t("sending") : t("faultyConfirm")}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

/** OK at once, or Faulty through its dialog: an admin's answer to an audit sample. */
export function SampleAnswer({ project, sample }: { project: string; sample: MergeSample }) {
  const t = useTranslations("mergeQueue.sample");
  const [faulty, setFaulty] = useState(false);
  const write = useAnswerSample(project);
  const failure = useWriteFailure();
  const ok = async () => {
    if (write.isPending) return;
    try {
      await write.mutateAsync({ sampleId: sample.id, result: "ok", note: null });
      notify({ tone: "success", text: t("okDone", { name: sampleName(sample) }) });
    } catch (error) {
      notifyFailure(t("failed", { name: sampleName(sample) }), failure(error, { 409: t("answered"), 403: t("forbidden") }));
    }
  };
  return (
    <div className="flex flex-wrap items-center gap-2">
      <Button
        type="button"
        variant="secondary"
        size="sm"
        busy={write.isPending}
        onClick={() => void ok()}
        aria-label={t("okLabel", { name: sampleName(sample) })}
        data-testid="sample-ok"
      >
        <CircleCheck aria-hidden="true" />
        {t("ok")}
      </Button>
      <Button
        type="button"
        variant="quiet-danger"
        size="sm"
        onClick={() => setFaulty(true)}
        aria-label={t("faultyLabel", { name: sampleName(sample) })}
        data-testid="sample-faulty"
      >
        <Bug aria-hidden="true" />
        {t("faulty")}
      </Button>
      <FaultyDialog project={project} sample={sample} open={faulty} onOpenChange={setFaulty} />
    </div>
  );
}

/**
 * The audit samples of the queue, those waiting for an answer first: the merge request drawn, a link to it on GitLab,
 * and for an admin OK or Faulty; an answered one says who answered, when, and their note.
 */
export function SamplesPanel({ project, samples, admin }: { project: string; samples: readonly MergeSample[]; admin: boolean }) {
  const t = useTranslations("mergeQueue.sample");
  const waiting = samples.filter((sample) => sample.result === null).length;
  return (
    <Panel title={t("title")} count={waiting} countWords={t("waitingCount", { count: waiting })} tone="attention" testId="samples">
      {samples.length === 0 ? (
        <p className="px-4 py-3 text-[13px] text-muted-foreground">{t("none")}</p>
      ) : (
        <ul className="flex flex-col">
          {samples.map((sample) => {
            const url = forgeUrl(sample.web_url);
            return (
              <li
                key={sample.id}
                className="flex min-w-0 flex-col gap-1.5 border-t px-4 py-3 first:border-t-0"
                data-testid="sample"
                data-sample-id={sample.id}
                data-result={sample.result ?? "waiting"}
              >
                <div className="flex min-w-0 flex-wrap items-center gap-2">
                  <SampleBadge result={sample.result} />
                  <span className="font-mono text-xs text-muted-foreground">{sampleName(sample)}</span>
                </div>
                <p className="text-[13px] font-medium text-pretty [overflow-wrap:anywhere]">
                  {url ? (
                    <a href={url} target="_blank" rel="noopener noreferrer" className={TEXT_LINK}>
                      {sample.title ?? sampleName(sample)}
                      <ExternalLink className="ml-1 inline size-3.5 align-[-2px]" aria-hidden="true" />
                      <span className="sr-only">{t("opensForge")}</span>
                    </a>
                  ) : (
                    (sample.title ?? sampleName(sample))
                  )}
                </p>
                {sample.result === null ? (
                  <>
                    <p className="text-xs text-fg-subtle">{t.rich("drawn", { when: () => <When value={sample.created_at} short /> })}</p>
                    {admin ? <SampleAnswer project={project} sample={sample} /> : null}
                  </>
                ) : (
                  <p className="text-xs text-fg-subtle [overflow-wrap:anywhere]">
                    {t.rich("answeredBy", { login: sample.answered_by ?? "?", when: () => <When value={sample.answered_at} short /> })}
                    {sample.note ? ` ${t("noteQuoted", { note: sample.note })}` : ""}
                  </p>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </Panel>
  );
}
