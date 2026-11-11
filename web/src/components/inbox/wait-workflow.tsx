"use client";

import { Ban, ChevronRight, CircleCheck, CircleDashed, CircleMinus, CircleX, ExternalLink, type LucideIcon } from "lucide-react";
import { useTranslations } from "next-intl";
import { useState } from "react";

import { useWaitSpan } from "@/components/status/wait-pill";
import { cn } from "@/lib/utils";
import {
  type ActionsState,
  actionsState,
  isOver,
  jobOpens,
  shortSha,
  stepsTotal,
  type WorkflowJob,
  type WorkflowRunWait,
  type WorkflowStep,
  workflowProgress,
} from "@/lib/waits";

/**
 * The source card of a wait on a run of a GitHub Actions workflow (web/DESIGN.md, The wait panel): the workflow, its
 * repo, branch and commit, its state, how long it ran and about how long it has left, the step running now, a bar of
 * its steps done, and its jobs, each folding open to its steps. A running or failed job starts open. Every job and step
 * links to its page on GitHub (a step to its job's page at `#step:N:1`). The time counts on in the browser from the
 * timestamps the hub read; the hub reads the run again on each pass of its reaper, and the page reads the wait as often
 * as it reads the run's decisions.
 */
export function WorkflowCard({ run, now }: { run: WorkflowRunWait; now: number | null }) {
  const t = useTranslations("wait.workflow");
  const span = useWaitSpan();
  const progress = workflowProgress(run, now);
  const { state, done, total, current, failed, elapsedMs, leftMs } = progress;
  const name = run.workflow ?? run.title ?? `#${run.run_id}`;
  const sha = shortSha(run.sha);
  const percent = total > 0 ? Math.round((done / total) * 100) : isOver(state) ? 100 : 0;
  const stateWords = t(`state.${state}`);
  const timeWords = elapsedMs === null ? null : isOver(state) ? t("took", { elapsed: span(elapsedMs) }) : leftMs !== null ? t("eta", { elapsed: span(elapsedMs), left: span(leftMs) }) : t("soFar", { elapsed: span(elapsedMs) });

  return (
    <div className="flex min-w-0 flex-col gap-3" data-testid="wait-workflow" data-state={state}>
      <div className="flex min-w-0 flex-wrap items-baseline gap-x-2 gap-y-1">
        <span className="text-[15px] leading-[22px] font-semibold [overflow-wrap:anywhere]">
          {run.url ? (
            <a href={run.url} target="_blank" rel="noreferrer" className="rounded-xs underline decoration-foreground/30 underline-offset-2 hover:text-brand hover:decoration-current" data-testid="wait-workflow-link">
              {name}
              <span className="sr-only">{t("opensOnGitHub")}</span>
            </a>
          ) : (
            name
          )}
        </span>
        {run.title && run.title !== name ? <span className="min-w-0 text-[13px] text-muted-foreground [overflow-wrap:anywhere]">{run.title}</span> : null}
      </div>
      <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-4 gap-y-1 text-[13px] leading-[18px]">
        <dt className="text-muted-foreground">{t("repo")}</dt>
        <dd className="font-mono [overflow-wrap:anywhere]">{run.repo}</dd>
        {run.branch ? (
          <>
            <dt className="text-muted-foreground">{t("branch")}</dt>
            <dd className="font-mono [overflow-wrap:anywhere]">{run.branch}</dd>
          </>
        ) : null}
        {sha ? (
          <>
            <dt className="text-muted-foreground">{t("commit")}</dt>
            <dd className="font-mono">{sha}</dd>
          </>
        ) : null}
        <dt className="text-muted-foreground">{t("status")}</dt>
        <dd data-testid="wait-workflow-status">
          <span className={cn("font-medium", STATE_TEXT[state])}>{stateWords}</span>
          {timeWords ? (
            <span className="text-muted-foreground tabular-nums" title={leftMs !== null ? t("etaTitle", { workflow: name }) : undefined} data-testid="wait-workflow-time">
              {", "}
              {timeWords}
            </span>
          ) : null}
        </dd>
      </dl>
      {current ? (
        <p className="text-[13px] leading-[18px] [overflow-wrap:anywhere]" data-testid="wait-workflow-now">
          {t("now", { n: current.step.number, total: stepsTotal(current.job), job: current.job.name ?? `#${current.job.id}`, name: current.step.name ?? "" })}
        </p>
      ) : failed ? (
        <p className="text-[13px] leading-[18px] text-danger [overflow-wrap:anywhere]" data-testid="wait-workflow-failed">
          {t("failedAt", { n: failed.step.number, name: failed.step.name ?? "" })}
        </p>
      ) : null}
      {total > 0 ? (
        <div className="flex flex-col gap-1">
          <div
            role="progressbar"
            aria-valuemin={0}
            aria-valuemax={total}
            aria-valuenow={done}
            aria-valuetext={t("bar", { done, total })}
            aria-label={t("barLabel", { workflow: name })}
            className="h-1.5 overflow-hidden rounded-full bg-muted"
            data-testid="wait-workflow-bar"
          >
            <div className={cn("h-full rounded-full transition-[width] duration-500 motion-reduce:transition-none", BAR[state])} style={{ width: `${percent}%` }} />
          </div>
          <p className="text-xs text-muted-foreground tabular-nums">{t("bar", { done, total })}</p>
        </div>
      ) : null}
      {run.jobs.length > 0 ? (
        <ul className="flex flex-col divide-y rounded-md border" data-testid="wait-workflow-jobs">
          {run.jobs.map((job) => (
            <JobRow key={job.id} job={job} />
          ))}
        </ul>
      ) : (
        <p className="text-[13px] text-muted-foreground">{t("noJobs")}</p>
      )}
      {run.jobs_total !== null && run.jobs_total > run.jobs.length ? (
        <p className="text-xs text-muted-foreground">{t("moreJobs", { count: run.jobs_total - run.jobs.length })}</p>
      ) : null}
    </div>
  );
}

const STATE_TEXT: Record<ActionsState, string> = {
  done: "text-success",
  running: "text-running",
  failed: "text-danger",
  skipped: "text-muted-foreground",
  cancelled: "text-muted-foreground",
  queued: "text-muted-foreground",
};

const BAR: Record<ActionsState, string> = {
  done: "bg-success-solid",
  running: "bg-running",
  failed: "bg-danger-solid",
  skipped: "bg-neutral-solid",
  cancelled: "bg-neutral-solid",
  queued: "bg-neutral-solid",
};

const ICON: Record<Exclude<ActionsState, "running">, LucideIcon> = {
  done: CircleCheck,
  failed: CircleX,
  skipped: CircleMinus,
  cancelled: Ban,
  queued: CircleDashed,
};

/** A state's mark: the live dot while it runs, its icon otherwise, in its tone. */
function StateMark({ state }: { state: ActionsState }) {
  if (state === "running") {
    return (
      <span className="relative mx-1 inline-flex size-2 shrink-0 rounded-full bg-running" aria-hidden="true">
        <span className="absolute inset-0 animate-live-ping rounded-full bg-running" />
      </span>
    );
  }
  const Icon = ICON[state];
  return <Icon className={cn("size-4 shrink-0", STATE_TEXT[state])} aria-hidden="true" />;
}

/** What a job's row says under its name: its steps done, or where it failed, or that it is queued or skipped. */
function useJobSub() {
  const t = useTranslations("wait.workflow");
  return (job: WorkflowJob, state: ActionsState): string => {
    const total = stepsTotal(job);
    if (state === "failed") {
      const step = job.steps.find((one) => actionsState(one.status, one.conclusion) === "failed");
      return step ? t("failedAt", { n: step.number, name: step.name ?? "" }) : t("state.failed");
    }
    if (state === "skipped") return t("state.skipped");
    if (state === "cancelled") return t("state.cancelled");
    if (state === "queued") return t("state.queued");
    if (state === "running") return t("jobSteps", { done: job.steps.filter((step) => isOver(actionsState(step.status, step.conclusion))).length, total });
    return t("jobStepsAll", { total });
  };
}

/** One job: its name, state and steps done, folding open to its steps; the link beside it opens it on GitHub. */
function JobRow({ job }: { job: WorkflowJob }) {
  const t = useTranslations("wait.workflow");
  const sub = useJobSub();
  const [open, setOpen] = useState(() => jobOpens(job));
  const state = actionsState(job.status, job.conclusion);
  const name = job.name ?? `#${job.id}`;
  return (
    <li className="flex min-w-0 items-start gap-1" data-testid="wait-job" data-state={state} data-open={open ? "true" : "false"}>
      <details className="group min-w-0 flex-1" open={open} onToggle={(event) => setOpen(event.currentTarget.open)}>
        <summary className="flex min-h-10 cursor-pointer list-none items-center gap-2 rounded-md px-3 py-2 hover:bg-accent max-md:min-h-11 [&::-webkit-details-marker]:hidden">
          <ChevronRight className="size-4 shrink-0 text-muted-foreground transition-transform group-open:rotate-90 motion-reduce:transition-none" aria-hidden="true" />
          <StateMark state={state} />
          <span className="flex min-w-0 flex-col">
            <span className="text-[13px] leading-[18px] font-medium [overflow-wrap:anywhere]">{name}</span>
            <span className="text-xs text-muted-foreground tabular-nums">
              <span className="sr-only">{t(`state.${state}`)}, </span>
              {sub(job, state)}
            </span>
          </span>
        </summary>
        {job.steps.length > 0 ? (
          <ol className="flex flex-col pb-2 pl-9" data-testid="wait-steps">
            {job.steps.map((step) => (
              <StepRow key={step.number} step={step} />
            ))}
          </ol>
        ) : null}
      </details>
      {job.url ? (
        <a
          href={job.url}
          target="_blank"
          rel="noreferrer"
          className="m-1 inline-flex size-8 shrink-0 items-center justify-center rounded-md text-muted-foreground hover:bg-accent hover:text-foreground max-md:size-11"
          aria-label={t("openJob", { job: name })}
          data-testid="wait-job-link"
        >
          <ExternalLink className="size-4" aria-hidden="true" />
        </a>
      ) : null}
    </li>
  );
}

/** One step of a job: its number, name and state, a link to it on GitHub. */
function StepRow({ step }: { step: WorkflowStep }) {
  const t = useTranslations("wait.workflow");
  const state = actionsState(step.status, step.conclusion);
  const words = (
    <>
      <StateMark state={state} />
      <span className="min-w-0 [overflow-wrap:anywhere]">
        <span className="tabular-nums text-muted-foreground">{step.number}.</span> {step.name ?? ""}
      </span>
    </>
  );
  return (
    <li data-testid="wait-step" data-state={state}>
      {step.url ? (
        <a href={step.url} target="_blank" rel="noreferrer" className="flex min-h-8 items-center gap-2 rounded-md px-2 text-[13px] hover:bg-accent hover:text-brand max-md:min-h-11">
          {words}
          <span className="sr-only">{t("stepLink", { state: t(`state.${state}`) })}</span>
        </a>
      ) : (
        <span className="flex min-h-8 items-center gap-2 px-2 text-[13px]">
          {words}
          <span className="sr-only">, {t(`state.${state}`)}</span>
        </span>
      )}
    </li>
  );
}
