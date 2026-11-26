"use client";

import { useQuery } from "@tanstack/react-query";
import { Ban, CircleCheck, CircleDashed, CircleDot, CircleHelp, CircleSlash } from "lucide-react";
import Link from "next/link";
import { useFormatter, useTranslations } from "next-intl";
import { type ReactNode, useState } from "react";

import { ProblemChips } from "@/components/data/problem-chips";
import { stepHref } from "@/components/plans/links";
import { planRunPhase } from "@/components/runs/model";
import { CostCell } from "@/components/runs/run-cost-duration";
import { type Run, runHref, runsQuery } from "@/components/runs/queries";
import { StatusBadge } from "@/components/status/status-badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { browserApi } from "@/lib/api/browser";
import { PEEK_RUNS, type StripStatus, stripStatus } from "@/lib/plan-list";
import { type Plan, type PlanStep, type PlanSummary, parsePlan, stepLabel } from "@/lib/plans";
import { cn } from "@/lib/utils";

/*
 * The sections of the plans list's peek panel (plan-peek.tsx): the facts, the goal, the steps and the recent plan
 * runs. They live under the route because they join the plans and the runs features.
 */

/** How much of the goal the panel shows before Read all. */
const GOAL_CUT = 280;

const STEP_ICON: Record<StripStatus, { icon: typeof CircleCheck; tone: string }> = {
  done: { icon: CircleCheck, tone: "text-success" },
  in_progress: { icon: CircleDot, tone: "text-running" },
  blocked: { icon: Ban, tone: "text-danger" },
  pending: { icon: CircleDashed, tone: "text-fg-subtle" },
  descoped: { icon: CircleSlash, tone: "text-fg-subtle" },
  other: { icon: CircleHelp, tone: "text-fg-subtle" },
};

/** The status a step has as written, for its mark: one of the four, or what the body says (descoped, ...). */
export function writtenStatus(step: PlanStep): string | null {
  return step.group === "other" ? step.rawStatus : step.group;
}

export function Section({ title, children, testId }: { title: string; children: ReactNode; testId?: string }) {
  return (
    <section className="flex flex-col gap-2 border-b px-4 py-3 last:border-b-0" data-testid={testId}>
      <h3 className="text-xs leading-4 font-semibold text-muted-foreground">{title}</h3>
      {children}
    </section>
  );
}

export function Goal({ goal }: { goal: string }) {
  const t = useTranslations("plans.peek");
  const [all, setAll] = useState(false);
  const long = goal.length > GOAL_CUT;
  const shown = long && !all ? `${goal.slice(0, GOAL_CUT).trimEnd()}…` : goal;
  return (
    <div className="flex flex-col items-start gap-1">
      <p className="text-sm whitespace-pre-line text-pretty [overflow-wrap:anywhere]" data-testid="plan-peek-goal" data-full={!long || all}>
        {shown}
      </p>
      {long ? (
        <Button type="button" variant="link" size="sm" className="h-auto p-0" aria-expanded={all} onClick={() => setAll(!all)} data-testid="plan-peek-goal-toggle">
          {all ? t("goalLess") : t("goalAll")}
        </Button>
      ) : null}
    </div>
  );
}

/** The steps in plan order, each with its mark; the step a plan run is at carries the run's state. */
export function Steps({ project, planId, steps, planRun }: { project: string; planId: string; steps: PlanStep[]; planRun: Run | null }) {
  const t = useTranslations("plans.peek");
  const tPhase = useTranslations("runs.planRun.phase");
  const at = planRun ? steps.find((step) => !["done", "descoped"].includes(writtenStatus(step) ?? "pending")) ?? null : null;
  if (steps.length === 0) return <p className="text-sm text-muted-foreground">{t("noSteps")}</p>;
  return (
    <ol className="flex flex-col" data-testid="plan-peek-steps">
      {steps.map((step) => {
        const status = stripStatus(writtenStatus(step));
        const { icon: Icon, tone } = STEP_ICON[status];
        const here = at?.key === step.key && planRun;
        return (
          <li key={step.key} className="flex min-w-0 flex-col gap-1 py-1" data-testid="plan-peek-step" data-step={step.key} data-status={status}>
            <div className="flex min-w-0 items-start gap-2">
              <Icon className={cn("mt-0.5 size-4 shrink-0", tone)} aria-hidden="true" />
              <span className="sr-only">{t(`status.${status}`)}: </span>
              <Link
                href={stepHref(project, planId, step.key)}
                className={cn(
                  "min-w-0 text-sm leading-5 [overflow-wrap:anywhere] transition-colors hover:text-brand",
                  status === "done" || status === "descoped" ? "text-muted-foreground" : "text-foreground",
                  status === "descoped" && "line-through",
                )}
              >
                <span className="mr-1.5 font-mono text-xs text-fg-subtle tabular-nums">{step.key}</span>
                {stepLabel(step)}
              </Link>
            </div>
            {here ? (
              <p className="ml-6 flex flex-wrap items-center gap-1.5 text-xs text-muted-foreground" data-testid="plan-peek-run-here">
                <StatusBadge kind="run" status={planRunPhase(planRun.state)} label={tPhase(`${planRunPhase(planRun.state)}Long`)} />
                <Link href={runHref(project, planRun.id)} className="font-mono hover:text-brand">
                  {t("runHere", { id: planRun.id })}
                </Link>
              </p>
            ) : null}
          </li>
        );
      })}
    </ol>
  );
}

/** The plan's latest plan runs: state, when, why it failed, what it cost. */
export function Runs({ project, planId }: { project: string; planId: string }) {
  const t = useTranslations("plans.peek");
  const format = useFormatter();
  const runs = useQuery(runsQuery(browserApi, project, { planId, kind: "plan", limit: PEEK_RUNS }));
  if (runs.isPending) return <Skeleton className="h-16 w-full" />;
  if (runs.isError) return <p className="text-sm text-muted-foreground">{t("runsError")}</p>;
  const list = runs.data.runs.slice(0, PEEK_RUNS);
  if (list.length === 0) return <p className="text-sm text-muted-foreground" data-testid="plan-peek-no-runs">{t("noRuns")}</p>;
  return (
    <ul className="flex flex-col divide-y" data-testid="plan-peek-runs">
      {list.map((run) => {
        const at = run.finished_at ?? run.started_at ?? run.queued_at;
        const why = run.state === "failed" || run.state === "lost" ? (run.error ?? run.failure_cause ?? null) : null;
        return (
          <li key={run.id} className="flex min-w-0 flex-col gap-1 py-2" data-testid="plan-peek-run" data-run-id={run.id} data-state={run.state}>
            <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
              <Link href={runHref(project, run.id)} className="font-mono text-[13px] font-medium tabular-nums hover:text-brand" aria-label={t("openRun", { id: run.id })}>
                #{run.id}
              </Link>
              <StatusBadge kind="run" status={run.state} />
              <time dateTime={at} className="text-xs text-muted-foreground tabular-nums">
                {format.dateTime(new Date(at), { dateStyle: "medium", timeStyle: "short" })}
              </time>
              <span className="ml-auto">
                <CostCell run={run} />
              </span>
            </div>
            {why ? (
              <p className="line-clamp-2 text-xs text-danger [overflow-wrap:anywhere]" title={why} data-testid="plan-peek-run-why">
                {why}
              </p>
            ) : null}
          </li>
        );
      })}
    </ul>
  );
}

export function Facts({ plan, summary, project }: { plan: Plan; summary: PlanSummary | null; project: string }) {
  const t = useTranslations("plans.peek");
  const format = useFormatter();
  const view = parsePlan(plan.body, plan.plan_id);
  const repos = summary?.repos ?? [...new Set([...view.repos.map((repo) => repo.repo), ...view.steps.flatMap((step) => (step.repo ? [step.repo] : []))])];
  const problems = summary?.problems ?? [];
  const row = "grid grid-cols-[6.5rem_minmax(0,1fr)] items-baseline gap-2";
  return (
    <dl className="flex flex-col gap-1.5 text-sm" data-testid="plan-peek-facts">
      <div className={row}>
        <dt className="text-xs text-muted-foreground">{t("changed")}</dt>
        <dd className="flex min-w-0 flex-wrap items-baseline gap-x-2">
          <span className="font-mono text-xs" data-testid="plan-peek-owner">{plan.updated_by}</span>
          <time dateTime={plan.updated_at} className="text-xs text-muted-foreground tabular-nums">
            {format.dateTime(new Date(plan.updated_at), { dateStyle: "medium", timeStyle: "short" })}
          </time>
        </dd>
      </div>
      <div className={row}>
        <dt className="text-xs text-muted-foreground">{t("problems")}</dt>
        <dd className="min-w-0">{problems.length > 0 ? <ProblemChips project={project} codes={problems} /> : <span className="text-fg-subtle">-</span>}</dd>
      </div>
      <div className={row}>
        <dt className="text-xs text-muted-foreground">{t("repos")}</dt>
        <dd className="flex min-w-0 flex-wrap gap-x-2 font-mono text-xs" data-testid="plan-peek-repos">
          {repos.length > 0 ? repos.map((repo) => <span key={repo}>{repo}</span>) : <span className="text-fg-subtle">-</span>}
        </dd>
      </div>
    </dl>
  );
}
