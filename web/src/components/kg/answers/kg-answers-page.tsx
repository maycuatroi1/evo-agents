"use client";

import { CircleAlert, Clock, Download, MessagesSquare } from "lucide-react";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { useState } from "react";

import { Ago } from "@/components/data/ago";
import { Fact, Facts } from "@/components/data/facts";
import { Meter } from "@/components/data/meter";
import { Banner } from "@/components/feedback/banner";
import { useWriteFailure } from "@/components/feedback/notice";
import { notify, notifyFailure } from "@/components/feedback/toast";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";
import { answeredShare, answerLines, answersFile, type KgAnswerJob, kgAnswerJobQuery } from "@/lib/kg/answer-queries";
import { initRunHref } from "@/lib/kg/routes";

import { KgHeader } from "../kg-header";

const LINK = "rounded-xs font-medium text-brand underline-offset-4 hover:text-brand-hover hover:underline";

/** Fetch every answer of the job and let the browser save them as answers.jsonl, as `hub kg answer` writes it. */
function DownloadButton({ project, job }: { project: string; job: KgAnswerJob }) {
  const t = useTranslations("kgAnswers");
  const failure = useWriteFailure();
  const [busy, setBusy] = useState(false);
  const save = async () => {
    setBusy(true);
    try {
      const lines = await answerLines(browserApi, project, job.id);
      const url = URL.createObjectURL(new Blob([answersFile(lines)], { type: "application/x-ndjson;charset=utf-8" }));
      const link = document.createElement("a");
      link.href = url;
      link.download = "answers.jsonl";
      link.click();
      URL.revokeObjectURL(url);
      notify({ tone: "success", text: t("downloaded", { count: lines.length }), description: link.download });
    } catch (error) {
      notifyFailure(t("downloadFailed"), failure(error));
    } finally {
      setBusy(false);
    }
  };
  return (
    <Button type="button" busy={busy} onClick={() => void save()} data-testid="kg-answers-download">
      <Download aria-hidden="true" />
      {job.state === "running" ? t("downloadSoFar") : t("download")}
    </Button>
  );
}

function JobCard({ project, job }: { project: string; job: KgAnswerJob }) {
  const t = useTranslations("kgAnswers");
  const share = answeredShare(job);
  const tone = job.state === "failed" ? "danger" : job.errors > 0 ? "warning" : "normal";
  return (
    <section className="flex flex-col gap-4 rounded-md border bg-card p-4 shadow-raised" data-testid="kg-answers-job" data-state={job.state}>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h2 className="text-base font-semibold">{t("job", { id: job.id, state: t(`states.${job.state}`) })}</h2>
        <DownloadButton project={project} job={job} />
      </div>
      <div className="flex flex-col gap-1.5">
        <Meter value={share} tone={tone} label={t("progress")} valueText={t("ended", { ended: job.answered + job.errors, questions: job.questions })} />
        <p className="text-sm tabular-nums" data-testid="kg-answers-counts">
          {t("counts", { answered: job.answered, errors: job.errors, asking: job.asking, pending: job.pending, questions: job.questions })}
        </p>
      </div>
      {job.error ? (
        <Banner icon={CircleAlert} tone="danger" title={t("failed")} testId="kg-answers-error">
          <p>{job.error}</p>
        </Banner>
      ) : job.note ? (
        <Banner icon={Clock} tone="attention" title={t("waits")} testId="kg-answers-note">
          <p>{job.note}</p>
        </Banner>
      ) : null}
      <Facts className="px-0 py-0">
        <Fact label={t("facts.batch")}>{job.batch}</Fact>
        <Fact label={t("facts.runs")}>
          {job.runs}
          {job.run_id != null ? (
            <>
              {" ("}
              <Link href={initRunHref(project, job.run_id)} className={LINK} data-testid="kg-answers-run-link">
                {t("facts.current", { id: job.run_id })}
              </Link>
              {")"}
            </>
          ) : null}
        </Fact>
        <Fact label={t("facts.created")}>
          <Ago value={job.created_at} never={t("facts.never")} />
        </Fact>
        <Fact label={t("facts.ended")}>
          <Ago value={job.ended_at ?? null} never={t("facts.notYet")} />
        </Fact>
      </Facts>
    </section>
  );
}

/**
 * /p/{project}/kg/answers/{id}: where an answer job of `evo-agents hub kg answer` stands, asked again while it runs:
 * its questions answered, with an error, being asked and waiting, its batches' runs, and why it waits or failed. The
 * button saves the answers so far, or all of them once it ended, as answers.jsonl in the file's order.
 */
export function KgAnswersPage({ project, id, error }: { project: string; id: number; error: ApiErrorInfo | null }) {
  const t = useTranslations("kgAnswers");
  const job = useHubQuery(kgAnswerJobQuery(browserApi, project, id), error);
  return (
    <div className="flex flex-col gap-6">
      <KgHeader project={project} current="answers" />
      <p className="flex max-w-prose items-start gap-2 text-sm text-muted-foreground">
        <MessagesSquare className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
        {t("intro")}
      </p>
      <QueryView state={job} loading={<Skeleton className="h-64 w-full rounded-md" />}>
        {(data) => <JobCard project={project} job={data} />}
      </QueryView>
    </div>
  );
}

export function KgAnswersSkeleton() {
  return (
    <div className="flex flex-col gap-6">
      <Skeleton className="h-16 w-full rounded-md" />
      <Skeleton className="h-64 w-full rounded-md" />
    </div>
  );
}
