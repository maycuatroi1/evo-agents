"use client";

import { CircleCheck, CircleX, FileDiff, GitBranch, TriangleAlert } from "lucide-react";
import Link from "next/link";
import { useFormatter, useTranslations } from "next-intl";
import { type ReactNode, useId } from "react";

import { Identifier } from "@/components/data/identifier";
import { useNow } from "@/components/kg/use-now";
import { Badge } from "@/components/ui/badge";
import { workerHref } from "@/components/workers/queries";

import { runTiming } from "./model";
import { HELD_STATES, isActiveState, type Run, runDiffHref, runHref } from "./queries";
import { leaseSecondsLeft, readDiffstat, readUsage, readVerify, type RunViewer, shortSha } from "./run-model";
import { useDuration } from "./runs-table";

/** The run's facts and its result, the two cards beside the log. */

function Card({ title, children, testId }: { title: string; children: ReactNode; testId: string }) {
  const id = useId();
  return (
    <section className="flex min-w-0 flex-col rounded-md border bg-card shadow-raised" aria-labelledby={id} data-testid={testId}>
      <h2 id={id} className="border-b px-4 py-3 text-[15px] leading-[22px] font-semibold">
        {title}
      </h2>
      <div className="min-w-0 px-4 py-3">{children}</div>
    </section>
  );
}

function Fact({ label, children, testId }: { label: string; children: ReactNode; testId?: string }) {
  return (
    <>
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="min-w-0 [overflow-wrap:anywhere]" data-testid={testId}>
        {children}
      </dd>
    </>
  );
}

function When({ at }: { at: string }) {
  const format = useFormatter();
  const date = new Date(at);
  return (
    <time dateTime={at} title={format.dateTime(date, { dateStyle: "full", timeStyle: "medium" })} className="tabular-nums">
      {format.dateTime(date, { dateStyle: "medium", timeStyle: "medium" })}
    </time>
  );
}

function Lease({ run }: { run: Run }) {
  const t = useTranslations("runs.detail.facts");
  const now = useNow(run.lease_expires_at !== null);
  const left = leaseSecondsLeft(run, now);
  if (!run.lease_expires_at || !(HELD_STATES as readonly string[]).includes(run.state)) {
    return <span className="text-muted-foreground">{run.state === "queued" ? t("leaseNone") : t("leaseReleased")}</span>;
  }
  if (left === null) return <When at={run.lease_expires_at} />;
  return (
    <span className="tabular-nums" data-testid="run-lease">
      {left > 0 ? t("leaseLeft", { minutes: Math.floor(left / 60), seconds: left % 60 }) : t("leaseExpired")}
    </span>
  );
}

export function RunDetails({ run, viewer }: { run: Run; viewer: RunViewer | null }) {
  const t = useTranslations("runs.detail.facts");
  const tRun = useTranslations("runs");
  const tRuntime = useTranslations("runs.runtime");
  const duration = useDuration();
  const now = useNow(isActiveState(run.state));
  const timing = runTiming(run, now);
  const mayOpenWorker = viewer !== null && (viewer.admin || viewer.login === run.dispatched_by);

  return (
    <Card title={t("title")} testId="run-details">
      <dl className="grid grid-cols-[max-content_minmax(0,1fr)] gap-x-4 gap-y-2 text-sm">
        <Fact label={t("worker")} testId="run-worker">
          {run.worker_id !== null && run.worker ? (
            <Identifier value={run.worker} href={mayOpenWorker ? workerHref(run.worker_id) : undefined} />
          ) : (
            <span className="text-muted-foreground">{run.pinned_worker_id !== null ? tRun("pinnedWaiting") : tRun("noWorkerYet")}</span>
          )}
        </Fact>
        <Fact label={t("runtime")} testId="run-runtime">
          {run.runtime === "any"
            ? t("runtimeAny", { mode: t(`mode.${run.mode}`) })
            : run.requested_runtime === "any"
              ? t("runtimePicked", { runtime: tRuntime(run.runtime), mode: t(`mode.${run.mode}`) })
              : t("runtimeMode", { runtime: tRuntime(run.runtime), mode: t(`mode.${run.mode}`) })}
        </Fact>
        <Fact label={t("model")} testId="run-model">
          {run.model ? <span className="font-mono text-xs">{run.model}</span> : <span className="text-muted-foreground">{t("modelDefault")}</span>}
        </Fact>
        {run.kind === "plan" ? (
          <Fact label={t("repos")} testId="run-repos">
            <ul className="flex flex-col gap-1">
              {(run.repos ?? []).map((repo) => (
                <li key={repo.repo} className="flex min-w-0 flex-wrap items-center gap-x-1.5 gap-y-1">
                  <span className="font-mono text-xs">{repo.repo}</span>
                  {repo.branch ? (
                    <span className="inline-flex min-w-0 items-center gap-1 text-muted-foreground">
                      <GitBranch className="size-3 shrink-0" aria-hidden="true" />
                      <span className="sr-only">{t("on")} </span>
                      <Identifier value={repo.branch} />
                    </span>
                  ) : null}
                </li>
              ))}
              {(run.repos ?? []).length === 0 ? <li className="text-muted-foreground">{t("none")}</li> : null}
            </ul>
          </Fact>
        ) : (
          <Fact label={t("repo")}>
            <span className="font-mono text-xs">{run.repo}</span>
            {run.branch ? (
              <>
                {" "}
                <span className="text-muted-foreground">{t("on")}</span> <Identifier value={run.branch} />
              </>
            ) : null}
          </Fact>
        )}
        <Fact label={t("session")}>
          {run.session_id ? <Identifier value={run.session_id} copy copyLabel={t("copySession")} /> : <span className="text-muted-foreground">{t("none")}</span>}
        </Fact>
        <Fact label={t("lease")}>
          <Lease run={run} />
        </Fact>
        <Fact label={t("attempt")}>
          <span className="tabular-nums">{t("attemptValue", { attempt: run.attempt, max: run.max_attempts })}</span>
          {run.parent_run_id !== null ? (
            <>
              {", "}
              <Link href={runHref(run.project, run.parent_run_id)} className="text-brand underline-offset-4 hover:underline" data-testid="run-parent">
                {t("parent", { id: run.parent_run_id })}
              </Link>
            </>
          ) : null}
          {run.resume_of_run_id !== null ? (
            <>
              {", "}
              <Link href={runHref(run.project, run.resume_of_run_id)} className="text-brand underline-offset-4 hover:underline" data-testid="run-resumes">
                {t("resumes", { id: run.resume_of_run_id })}
              </Link>
            </>
          ) : null}
        </Fact>
        <Fact label={t("approval")}>{run.kind === "plan" ? t("approvalPlan") : t(`approvals.${run.approval}`)}</Fact>
        <Fact label={t("timeout")} testId="run-timeout">
          {run.kind === "plan" ? (
            <>
              <span>{t("timeoutHours", { hours: Math.round(run.timeout_min / 60) })}</span>
              <span className="block text-xs text-muted-foreground tabular-nums">
                {t("agentTime", { used: duration(run.run_seconds * 1000) })}
              </span>
            </>
          ) : (
            t("timeoutValue", { minutes: run.timeout_min })
          )}
        </Fact>
        <Fact label={t("dispatched")}>
          <span className="font-mono text-xs">{run.dispatched_by}</span>
          <span className="block text-xs text-muted-foreground">
            <When at={run.queued_at} />
          </span>
        </Fact>
        {timing.startedAt ? (
          <Fact label={t("started")}>
            <When at={timing.startedAt} />
          </Fact>
        ) : null}
        {run.finished_at ? (
          <Fact label={t("finished")}>
            <When at={run.finished_at} />
          </Fact>
        ) : null}
        {timing.durationMs !== null ? (
          <Fact label={timing.waiting ? t("waited") : t("duration")}>
            <span className="tabular-nums">{duration(timing.durationMs)}</span>
          </Fact>
        ) : null}
        <Fact label={t("revision")}>
          <Identifier value={t("revisionValue", { revision: run.plan_revision })} />
        </Fact>
      </dl>
    </Card>
  );
}

export function RunResult({ run }: { run: Run }) {
  const t = useTranslations("runs.detail.result");
  const format = useFormatter();
  const verify = readVerify(run.verify);
  const diffstat = readDiffstat(run.diffstat);
  const usage = readUsage(run.usage);
  const nothing = !run.error && verify.length === 0 && !run.commit_sha && !diffstat && !run.evidence && usage.length === 0 && !run.diff_sha256;

  return (
    <Card title={t("title")} testId="run-result">
      {nothing ? (
        <p className="text-sm text-pretty text-muted-foreground" data-testid="run-result-empty">
          {isActiveState(run.state) ? t("pending") : t("empty")}
        </p>
      ) : (
        <div className="flex flex-col gap-3 text-sm">
          {run.error ? (
            <p
              className="flex items-start gap-2 rounded-md border border-danger/30 bg-danger-soft px-3 py-2 text-danger"
              data-testid="run-error-text"
            >
              <TriangleAlert className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
              <span className="min-w-0 break-words whitespace-pre-wrap">{run.error}</span>
            </p>
          ) : null}
          <dl className="grid grid-cols-[max-content_minmax(0,1fr)] gap-x-4 gap-y-2">
            {verify.length ? (
              <Fact label={t("verify")} testId="run-verify">
                <ul className="flex flex-col gap-1.5">
                  {verify.map((item, index) => {
                    const passed = item.exitCode === 0;
                    return (
                      <li key={index} className="flex min-w-0 flex-col gap-0.5">
                        <span className="flex flex-wrap items-center gap-1.5">
                          <Badge variant={passed ? "success" : "destructive"}>
                            {passed ? <CircleCheck aria-hidden="true" /> : <CircleX aria-hidden="true" />}
                            {item.exitCode === null ? t("exitUnknown") : t("exit", { code: item.exitCode })}
                          </Badge>
                          {item.durationMs !== null ? (
                            <span className="text-xs text-muted-foreground tabular-nums">
                              {t("seconds", { seconds: format.number(item.durationMs / 1000, { maximumFractionDigits: 1 }) })}
                            </span>
                          ) : null}
                        </span>
                        <code className="font-mono text-xs [overflow-wrap:anywhere]">{item.command}</code>
                      </li>
                    );
                  })}
                </ul>
              </Fact>
            ) : null}
            {run.commit_sha ? (
              <Fact label={t("commit")} testId="run-commit">
                <Identifier value={run.commit_sha} title={run.commit_sha} copy copyLabel={t("copyCommit")}>
                  {shortSha(run.commit_sha)}
                </Identifier>
                {run.branch ? <span className="text-xs text-muted-foreground"> {t("onBranch", { branch: run.branch })}</span> : null}
              </Fact>
            ) : null}
            {diffstat || run.diff_sha256 ? (
              <Fact label={t("changes")} testId="run-diffstat">
                {diffstat ? (
                  <span className="tabular-nums">
                    {t("diffstat", { files: diffstat.files, insertions: format.number(diffstat.insertions), deletions: format.number(diffstat.deletions) })}
                  </span>
                ) : null}
                {run.diff_sha256 ? (
                  <Link
                    href={runDiffHref(run.project, run.id)}
                    className="mt-0.5 flex w-fit items-center gap-1 text-brand underline-offset-4 hover:underline"
                    data-testid="run-diff-link"
                  >
                    <FileDiff className="size-3.5" aria-hidden="true" />
                    {t("viewDiff")}
                  </Link>
                ) : null}
              </Fact>
            ) : null}
            {usage.length ? (
              <Fact label={t("usage")}>
                <ul className="flex flex-col font-mono text-xs">
                  {usage.map((item) => (
                    <li key={item.key} className="tabular-nums">
                      {item.key}: {format.number(item.value)}
                    </li>
                  ))}
                </ul>
              </Fact>
            ) : null}
          </dl>
          {run.evidence ? (
            <div className="flex flex-col gap-1">
              <h3 className="text-sm text-muted-foreground">{t("evidence")}</h3>
              <p
                className="max-h-60 overflow-y-auto rounded-md bg-muted/60 px-3 py-2 text-xs break-words whitespace-pre-wrap"
                tabIndex={0}
                data-testid="run-evidence"
              >
                {run.evidence}
              </p>
            </div>
          ) : null}
        </div>
      )}
    </Card>
  );
}
