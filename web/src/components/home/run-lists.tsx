"use client";

import { useQueryClient } from "@tanstack/react-query";
import { RotateCcw } from "lucide-react";
import Link from "next/link";
import { useTranslations } from "next-intl";
import type { ReactNode } from "react";

import { useWriteFailure } from "@/components/admin/notice";
import { Identifier, NAME_LINK, RunRef } from "@/components/data/identifier";
import { notify, notifyFailure } from "@/components/feedback/toast";
import { useNow } from "@/components/kg/use-now";
import { MONITOR_PATH } from "@/components/monitor/model";
import { StepBar } from "@/components/plans/progress";
import { PlanRunKindBadge } from "@/components/runs/badges";
import { useRunControl } from "@/components/runs/hooks";
import { runHref } from "@/components/runs/queries";
import { StatusBadge, StatusIcon } from "@/components/status/status-badge";
import { Button } from "@/components/ui/button";
import { workerHref } from "@/components/workers/queries";
import { queryKeys } from "@/lib/queries";
import { cn } from "@/lib/utils";

import { canRerun, decisionOfRun, isRunningState, type Overview, type OverviewRun, ranFor } from "./model";
import { CARD_LINK, HomeCard, LiveDot, Relative, Row, useDuration } from "./parts";

/** Home's In flight (the runs queued, at work, waiting or parked) and Recent (the runs that ended last, with Rerun). */

function RunTitle({ run }: { run: OverviewRun }) {
  const plan = run.kind === "plan" || run.step_key === null;
  const title = plan ? (run.plan_title ?? run.title ?? run.plan_id) : (run.title ?? run.step_key ?? run.plan_id);
  return (
    <>
      <Link href={runHref(run.project, run.id)} className={cn(NAME_LINK, "min-w-0 truncate")} title={title} data-testid="home-run-link" data-run-id={run.id}>
        {title}
      </Link>
      {run.kind === "plan" ? <PlanRunKindBadge /> : null}
    </>
  );
}

function useRunWhere() {
  const t = useTranslations("home.flight");
  return (run: OverviewRun) =>
    run.kind === "plan" || run.step_key === null
      ? t("wherePlan", { project: run.project, plan: run.plan_id })
      : t("where", { project: run.project, plan: run.plan_id, step: run.step_key });
}

function WorkerChip({ run, viewer }: { run: OverviewRun; viewer: string | null }) {
  const t = useTranslations("home.flight");
  if (run.worker_id === null || !run.worker) return <span>{t("noWorker")}</span>;
  // A worker's page opens for its owner (or a hub admin); the run's owner is the worker's owner.
  return <Identifier value={run.worker} href={viewer === run.dispatched_by ? workerHref(run.worker_id) : undefined} className="h-[18px]" />;
}

function PlanSteps({ run }: { run: OverviewRun }) {
  const t = useTranslations("home.flight");
  if (run.kind !== "plan" || run.steps_total === null || run.steps_done === null) return null;
  const done = run.steps_done;
  const total = run.steps_total;
  return (
    <span className="flex items-center gap-2" data-testid="home-plan-progress" data-done={done} data-total={total}>
      <StepBar counts={{ done, pending: Math.max(total - done, 0), in_progress: 0, blocked: 0, other: 0, total }} className="h-1.5 w-24 border-0" />
      <span aria-hidden="true" className="text-[13px] text-muted-foreground">
        {t("stepsShort", { done, total })}
      </span>
      <span className="sr-only">{t("steps", { done, total })}</span>
    </span>
  );
}

function InFlightEnd({ run, overview, viewer, now }: { run: OverviewRun; overview: Overview; viewer: string | null; now: number | null }) {
  const t = useTranslations("home.flight");
  const duration = useDuration();
  if (isRunningState(run.state)) {
    const ms = ranFor(run, now);
    return ms === null ? <StatusBadge kind="run" status={run.state} /> : <span data-testid="home-run-duration">{t("ran", { duration: duration(ms) })}</span>;
  }
  if (run.state === "waiting") {
    const decision = decisionOfRun(overview, run);
    const owner = decision?.owner ?? run.dispatched_by;
    return <StatusBadge kind="run" status="waiting" label={owner === viewer ? t("waitingYou") : t("waitingFor", { owner })} />;
  }
  if (run.state === "queued") {
    return (
      <>
        <StatusBadge kind="run" status="queued" />
        <span>{t.rich("queuedAgo", { time: () => <Relative value={run.queued_at} /> })}</span>
      </>
    );
  }
  return <StatusBadge kind="run" status={run.state} />;
}

export function InFlight({ overview, viewer }: { overview: Overview; viewer: string | null }) {
  const t = useTranslations("home.flight");
  const tRuntime = useTranslations("runs.runtime");
  const where = useRunWhere();
  const now = useNow(overview.active_runs.some((run) => isRunningState(run.state)));
  if (overview.active_runs.length === 0) return null;
  return (
    <HomeCard
      title={t("title")}
      count={{ value: overview.active_runs.length, tone: "running", words: t("count", { count: overview.active_runs.length }) }}
      action={
        <Link href={MONITOR_PATH} className={CARD_LINK} data-testid="in-flight-monitor">
          {t("monitor")}
        </Link>
      }
      testId="in-flight"
    >
      <ul>
        {overview.active_runs.map((run) => (
          <Row
            key={run.id}
            testId="in-flight-item"
            data={{ "run-id": run.id, state: run.state }}
            mark={run.state === "running" ? <LiveDot /> : <StatusIcon kind="run" status={run.state} />}
            title={<RunTitle run={run} />}
            sub={
              <>
                <span className="min-w-0 [overflow-wrap:anywhere]">
                  <RunRef id={run.id} className="mr-2 text-xs" />
                  {where(run)}
                </span>
                <WorkerChip run={run} viewer={viewer} />
                {run.runtime !== "any" ? <span>{tRuntime(run.runtime)}</span> : null}
              </>
            }
            end={
              <>
                <PlanSteps run={run} />
                <InFlightEnd run={run} overview={overview} viewer={viewer} now={now} />
              </>
            }
          />
        ))}
      </ul>
    </HomeCard>
  );
}

function RerunButton({ run }: { run: OverviewRun }) {
  const t = useTranslations("home.recent");
  const tToast = useTranslations("runs.detail.actions.toast");
  const tErrors = useTranslations("runs.detail.errors");
  const queryClient = useQueryClient();
  const control = useRunControl(run);
  const failure = useWriteFailure();
  return (
    <Button
      type="button"
      variant="ghost"
      size="sm"
      busy={control.isPending}
      aria-label={t("rerunLabel", { id: run.id })}
      onClick={() => {
        if (control.isPending) return;
        control.mutate("rerun", {
          onSuccess: (answer) =>
            void notify({
              tone: "success",
              text: tToast("rerun", { id: answer.id }),
              description: tToast("rerunText", { of: run.id }),
              link: { label: tToast("openRun"), href: runHref(answer.project, answer.id) },
            }),
          onError: (error) =>
            void notifyFailure(
              tToast("failed.rerun", { id: run.id }),
              failure(error, { 403: tErrors("forbidden"), 404: tErrors("notFound"), 409: tErrors("conflict") }),
            ),
          onSettled: () => void queryClient.invalidateQueries({ queryKey: queryKeys.overview }),
        });
      }}
      data-testid="recent-rerun"
    >
      <RotateCcw aria-hidden="true" />
      {control.isPending ? t("rerunning") : t("rerun")}
    </Button>
  );
}

function RecentSub({ run }: { run: OverviewRun }) {
  const t = useTranslations("home.recent");
  const where = useRunWhere();
  const duration = useDuration();
  const ms = ranFor(run, null);
  let outcome: ReactNode = null;
  if ((run.state === "failed" || run.state === "lost") && run.error) {
    outcome = (
      <span className="min-w-0 truncate text-danger" title={run.error} data-testid="recent-error">
        {run.error}
      </span>
    );
  } else if (run.state === "cancelled") {
    outcome = <span>{t("cancelled", { owner: run.dispatched_by })}</span>;
  } else if (ms !== null) {
    outcome = <span>{run.worker ? t("ranOn", { duration: duration(ms), worker: run.worker }) : t("ran", { duration: duration(ms) })}</span>;
  }
  return (
    <>
      <span className="min-w-0 [overflow-wrap:anywhere]">
        <RunRef id={run.id} className="mr-2 text-xs" />
        {where(run)}
      </span>
      {outcome}
    </>
  );
}

export function Recent({ overview, viewer }: { overview: Overview; viewer: string | null }) {
  const t = useTranslations("home.recent");
  return (
    <HomeCard title={t("title")} testId="recent">
      {overview.recent_runs.length === 0 ? (
        <p className="px-4 py-6 text-[13px] text-muted-foreground" data-testid="recent-empty">
          {t("empty")}
        </p>
      ) : (
        <ul>
          {overview.recent_runs.map((run) => (
            <Row
              key={run.id}
              testId="recent-item"
              data={{ "run-id": run.id, state: run.state }}
              mark={<StatusIcon kind="run" status={run.state} />}
              title={<RunTitle run={run} />}
              sub={<RecentSub run={run} />}
              end={
                <>
                  {canRerun(run, viewer, overview.projects) ? <RerunButton run={run} /> : null}
                  {run.finished_at ? <Relative value={run.finished_at} /> : null}
                </>
              }
            />
          ))}
        </ul>
      )}
    </HomeCard>
  );
}
