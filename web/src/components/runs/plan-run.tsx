"use client";

import { useQuery } from "@tanstack/react-query";
import { ArrowRight, CircleDot, MessageCircleQuestionMark, Play, Server, Workflow } from "lucide-react";
import Link from "next/link";
import { useFormatter, useTranslations } from "next-intl";
import { useId, useState } from "react";

import { useNow } from "@/components/kg/use-now";
import { stepHref } from "@/components/plans/links";
import { StatusBadge, useStatusText } from "@/components/status/status-badge";
import { Button } from "@/components/ui/button";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { browserApi } from "@/lib/api/browser";
import { type PlanStep, percent } from "@/lib/plans";
import { cn } from "@/lib/utils";

import { useCanDispatch, useViewer } from "./hooks";
import { activePlanRun, type PlanRunLock, planRunLock, planRunPhase } from "./model";
import { PlanRunDialog } from "./plan-run-dialog";
import {
  decisionHref,
  isActiveState,
  LIVE_REFRESH_MS,
  openDecisionsQuery,
  planActiveRunsQuery,
  type Run,
  runHref,
  runsQuery,
} from "./queries";

/**
 * The plan pages' side of plan runs: the active runs of a plan (polled every 5 seconds while one is active, every 30
 * otherwise), the Run plan button for a writer, and the banner that follows a plan run from the plan's page.
 */

/** The active runs of one plan and its plan run among them; `enabled` false for a plan id the API cannot take. */
export function usePlanActivity(project: string, planId: string, enabled = true) {
  const query = useQuery({ ...runsQuery(browserApi, project, planActiveRunsQuery(planId)), enabled });
  const active = (query.data?.runs ?? []).filter((run) => run.plan_id === planId && isActiveState(run.state));
  // A failed read locks nothing: the hub decides again on the dispatch.
  return { query, active, planRun: activePlanRun(active, planId), loaded: !query.isPending };
}

function useLockText() {
  const t = useTranslations("runs.planRun.lock");
  const tState = useStatusText("run");
  return (lock: PlanRunLock | "loading"): string => {
    if (lock === "loading") return t("loading");
    if (lock.kind === "planRun") return t("planRun", { id: lock.id, state: tState(lock.state) });
    if (lock.kind === "stepRun") return t("stepRun", { id: lock.id, state: tState(lock.state), step: lock.step });
    return t("noPending");
  };
}

/**
 * Run plan, for a writer of the project (whoami's grants; the API decides again). It is offered but locked, with the
 * reason beside it, while the plan has an active run or no step left to run; `pending` is how many steps are pending
 * (on the list, how many are not done). `layout` stack puts the reason under the button; row, on a table's row
 * that holds two lines at most, puts it in a tooltip on the button and in its description for screen readers.
 */
export function RunPlanButton({
  project,
  planId,
  pending,
  runs,
  loaded,
  onDispatched,
  size = "default",
  variant = "default",
  layout = "stack",
  testId = "run-plan",
}: {
  project: string;
  planId: string;
  pending: number;
  runs: readonly Pick<Run, "id" | "kind" | "plan_id" | "state" | "step_key">[];
  loaded: boolean;
  onDispatched: (run: Run) => void;
  /** 32 px in a page's head, `sm` on a table's rows. */
  size?: "sm" | "default";
  /** outline where a button repeats on every row of a table. */
  variant?: "default" | "outline";
  layout?: "stack" | "row";
  testId?: string;
}) {
  const t = useTranslations("runs.planRun");
  const ids = useId();
  const canDispatch = useCanDispatch(project);
  const lockText = useLockText();
  const [open, setOpen] = useState(false);
  if (!canDispatch) return null;
  const lock = loaded ? planRunLock(runs, planId, pending) : "loading";
  const locked = lock !== null;
  const button = (
    <Button
      type="button"
      size={size}
      variant={variant}
      className="shrink-0 aria-disabled:cursor-not-allowed aria-disabled:opacity-50"
      aria-disabled={locked || undefined}
      aria-describedby={locked ? `${ids}-lock` : undefined}
      onClick={() => {
        if (!locked) setOpen(true);
      }}
      data-testid={testId}
      data-locked={lock === null ? undefined : lock === "loading" ? "loading" : lock.kind}
    >
      <Play aria-hidden="true" />
      {t("button")}
    </Button>
  );
  const dialog = (
    <PlanRunDialog project={project} planId={planId} open={open} onOpenChange={setOpen} onDispatched={onDispatched} />
  );
  if (layout === "row") {
    return (
      <>
        {locked ? (
          <Tooltip>
            <TooltipTrigger asChild>{button}</TooltipTrigger>
            <TooltipContent side="left">{lockText(lock)}</TooltipContent>
          </Tooltip>
        ) : (
          button
        )}
        {locked ? (
          <span id={`${ids}-lock`} className="sr-only" data-testid={`${testId}-lock`}>
            {lockText(lock)}
          </span>
        ) : null}
        {dialog}
      </>
    );
  }
  return (
    <div className="flex min-w-0 flex-col items-start gap-1 sm:items-end">
      {button}
      {locked ? (
        <p id={`${ids}-lock`} className="max-w-56 text-xs text-pretty text-muted-foreground sm:text-right" data-testid={`${testId}-lock`}>
          {lockText(lock)}
        </p>
      ) : null}
      {dialog}
    </div>
  );
}

const BANNER_TONE = {
  queued: "border-border bg-card",
  running: "border-running/30 bg-running-soft",
  waiting: "border-attention/30 bg-attention-soft",
  parked: "border-border bg-muted/60",
  review: "border-review/30 bg-review-soft",
} as const;

/**
 * The plan's active plan run on its page: run #N on worker W, its state (Running, Waiting for your decision, Parked),
 * the steps done of the total and the steps in progress, with links to the run and to the decision it waits on. The
 * plan's steps come from the plan as the page reads it, which polls while the run is active.
 */
export function PlanRunBanner({ project, run, steps }: { project: string; run: Run; steps: readonly PlanStep[] }) {
  const t = useTranslations("runs.planRun.banner");
  const tPhase = useTranslations("runs.planRun.phase");
  const format = useFormatter();
  const ids = useId();
  const viewer = useViewer();
  const phase = planRunPhase(run.state);
  const decisions = useQuery({ ...openDecisionsQuery(browserApi, project, run.id), refetchInterval: LIVE_REFRESH_MS });
  const open = decisions.data?.decisions ?? [];
  const latest = open[0] ?? null;
  const now = useNow(phase === "waiting" || phase === "parked");
  const done = steps.filter((step) => step.group === "done").length;
  const total = steps.length;
  const working = steps.filter((step) => step.group === "in_progress");
  const owner = viewer?.login === run.dispatched_by;
  const since = phase === "waiting" ? run.waiting_since : phase === "parked" ? run.parked_at : null;
  const sinceText = since
    ? now === null
      ? format.dateTime(new Date(since), { dateStyle: "medium", timeStyle: "short" })
      : format.relativeTime(new Date(since), Math.max(now, Date.parse(since)))
    : null;
  const worker = run.worker ?? null;

  return (
    <section
      aria-labelledby={`${ids}-title`}
      className={cn("flex flex-col gap-3 rounded-md border p-4", BANNER_TONE[phase])}
      data-testid="plan-run-banner"
      data-run-id={run.id}
      data-phase={phase}
    >
      <div className="flex flex-col gap-3 md:flex-row md:items-start md:justify-between">
        <div className="flex min-w-0 flex-col gap-1.5">
          <h2 id={`${ids}-title`} className="flex flex-wrap items-center gap-x-2 gap-y-1 text-base font-medium">
            <Workflow className="size-4 shrink-0 text-muted-foreground" aria-hidden="true" />
            <span>{t("title", { id: run.id })}</span>
            <StatusBadge kind="run" status={phase} label={tPhase(`${phase}Long`)} data-testid="plan-run-phase" />
          </h2>
          <p className="flex flex-wrap items-center gap-x-1.5 gap-y-0.5 text-sm text-pretty text-muted-foreground" data-testid="plan-run-banner-worker">
            <Server className="size-3.5 shrink-0" aria-hidden="true" />
            <span className="min-w-0 [overflow-wrap:anywhere]">
              {worker
                ? t.rich("onWorker", { worker, login: run.dispatched_by, name: (chunks) => <span className="font-mono text-foreground">{chunks}</span> })
                : run.pinned_worker_id !== null
                  ? t("pinnedWaiting", { login: run.dispatched_by })
                  : t("noWorker", { login: run.dispatched_by })}
            </span>
          </p>
          {phase === "waiting" || phase === "parked" ? (
            <p className="text-sm text-pretty" data-testid="plan-run-banner-why">
              {phase === "waiting"
                ? t(owner ? "waitingOwner" : "waitingOther", { since: sinceText ?? "none", login: run.dispatched_by })
                : t(owner ? "parkedOwner" : "parkedOther", { since: sinceText ?? "none", login: run.dispatched_by })}
            </p>
          ) : null}
        </div>
        <div className="flex shrink-0 flex-wrap items-center gap-2">
          {latest ? (
            <Button asChild variant={phase === "waiting" || phase === "parked" ? "default" : "outline"}>
              <Link href={decisionHref(latest.id)} data-testid="plan-run-banner-decision">
                <MessageCircleQuestionMark aria-hidden="true" />
                {owner ? t("answer") : t("openDecision")}
                {open.length > 1 ? <span className="tabular-nums">({open.length})</span> : null}
              </Link>
            </Button>
          ) : null}
          <Button asChild variant="outline" className="bg-card">
            <Link href={runHref(project, run.id)} data-testid="plan-run-banner-link">
              {t("openRun", { id: run.id })}
              <ArrowRight aria-hidden="true" />
            </Link>
          </Button>
        </div>
      </div>
      {latest ? (
        <p className="line-clamp-2 text-sm text-pretty [overflow-wrap:anywhere]" data-testid="plan-run-banner-question">
          <span className="text-muted-foreground">{t("question")}</span> {latest.question}
        </p>
      ) : null}
      <div className="flex flex-col gap-2">
        <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1 text-sm">
          <span className="font-medium tabular-nums" data-testid="plan-run-banner-progress">
            {t("progress", { done, total })}
          </span>
          <span className="text-xs text-muted-foreground tabular-nums">{t("percent", { percent: percent(done, total) })}</span>
        </div>
        <div
          className="h-1.5 overflow-hidden rounded-full bg-border"
          role="progressbar"
          aria-valuemin={0}
          aria-valuemax={total}
          aria-valuenow={done}
          aria-label={t("progressLabel")}
        >
          <div className="h-full rounded-full bg-running transition-[width] duration-slow motion-reduce:transition-none" style={{ width: `${percent(done, total)}%` }} />
        </div>
        {working.length > 0 ? (
          <ul className="flex flex-col gap-1 text-sm" aria-label={t("inProgress")} data-testid="plan-run-banner-working">
            {working.map((step) => (
              <li key={step.key} className="flex min-w-0 items-start gap-1.5">
                <CircleDot className="mt-0.5 size-4 shrink-0 text-running" aria-hidden="true" />
                <span className="min-w-0 [overflow-wrap:anywhere]">
                  <span className="text-muted-foreground">{t("now")}</span>{" "}
                  <Link href={stepHref(project, run.plan_id, step.key)} className="text-brand underline-offset-4 hover:underline">
                    {t("step", { key: step.key, title: step.title ?? step.what?.split("\n")[0] ?? "" })}
                  </Link>
                </span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-xs text-muted-foreground">{phase === "queued" ? t("notStarted") : t("noStepInProgress")}</p>
        )}
      </div>
      <p className="sr-only" aria-live="polite">
        {t("live", { id: run.id, state: t(`state.${phase}`), done, total })}
      </p>
    </section>
  );
}
