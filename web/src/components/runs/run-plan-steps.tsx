"use client";

import { useQuery } from "@tanstack/react-query";
import { ChevronRight, Circle, CircleAlert, CircleCheck, CircleDot, CircleHelp, type LucideIcon } from "lucide-react";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { useId, useState } from "react";

import { stepHref } from "@/components/plans/links";
import { Skeleton } from "@/components/ui/skeleton";
import { browserApi } from "@/lib/api/browser";
import { planQuery } from "@/lib/plan-queries";
import { countSteps, parsePlan, percent, type PlanStep, stepLabel } from "@/lib/plans";
import { cn } from "@/lib/utils";

import { isActiveState, LIVE_REFRESH_MS, type Run } from "./queries";

const STEP_ICON: Record<PlanStep["group"], { icon: LucideIcon; tone: string }> = {
  done: { icon: CircleCheck, tone: "text-success" },
  in_progress: { icon: CircleDot, tone: "text-running" },
  blocked: { icon: CircleAlert, tone: "text-attention" },
  pending: { icon: Circle, tone: "text-fg-subtle" },
  other: { icon: CircleHelp, tone: "text-muted-foreground" },
};

function StepRow({ run, step }: { run: Run; step: PlanStep }) {
  const t = useTranslations("runs.detail.planSteps");
  const tStatus = useTranslations("status.step");
  const { icon: Icon, tone } = STEP_ICON[step.group];
  return (
    <li
      className={cn("flex min-w-0 items-start gap-2.5 rounded-md px-2 py-2", step.group === "in_progress" && "bg-accent")}
      aria-current={step.group === "in_progress" ? "step" : undefined}
      data-testid="run-plan-step"
      data-status={step.group}
    >
      <Icon className={cn("mt-0.5 size-4 shrink-0", tone)} aria-hidden="true" />
      <span className="sr-only">{step.group === "other" ? (step.rawStatus ?? "") : tStatus(step.group)}: </span>
      <span className="mt-px w-5 shrink-0 font-mono text-xs leading-[18px] text-fg-subtle tabular-nums">{step.key}</span>
      <Link
        href={stepHref(run.project, run.plan_id, step.key)}
        className="line-clamp-2 min-w-0 flex-1 text-sm leading-5 text-foreground underline-offset-4 [overflow-wrap:anywhere] hover:underline max-md:text-[15px] max-md:leading-[22px]"
      >
        {stepLabel(step) || t("untitled")}
      </Link>
      {step.repo ? <span className="mt-px shrink-0 font-mono text-xs leading-[18px] text-fg-subtle max-md:hidden">{step.repo}</span> : null}
    </li>
  );
}

/** A folded run of steps, done or to do: its count, and the steps once opened. */
function StepFold({ run, steps, label, testId }: { run: Run; steps: PlanStep[]; label: string; testId: string }) {
  const [open, setOpen] = useState(false);
  if (steps.length === 0) return null;
  return (
    <li className="flex min-w-0 flex-col">
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen((shown) => !shown)}
        className="group/fold flex min-h-9 w-full cursor-pointer items-center gap-2.5 rounded-md px-2 text-left text-sm text-muted-foreground transition-colors hover:bg-accent hover:text-foreground focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-ring max-md:min-h-11"
        data-testid={testId}
      >
        <ChevronRight className="size-4 shrink-0 transition-transform group-aria-expanded/fold:rotate-90" aria-hidden="true" />
        {label}
      </button>
      {open ? (
        <ol className="flex flex-col">
          {steps.map((step) => (
            <StepRow key={step.key} run={run} step={step} />
          ))}
        </ol>
      ) : null}
    </li>
  );
}

/**
 * The steps of a plan run's plan, as the hub holds the plan now, grouped by status as Buildkite groups its jobs: the
 * steps in progress, blocked or of another status first, one row each (a 16 px status icon, the key, the title as a
 * link), then "6 done" and "14 to do" as rows that open on their steps. The count done heads it. The run reports each
 * step as it goes, so the plan is read again every 5 seconds while the run is active.
 */
export function RunPlanSteps({ run }: { run: Run }) {
  const t = useTranslations("runs.detail.planSteps");
  const ids = useId();
  const { pending, view, counts } = usePlanProgress(run);
  const steps = view?.steps ?? [];
  const now = steps.filter((step) => step.group === "in_progress" || step.group === "blocked" || step.group === "other");
  const done = steps.filter((step) => step.group === "done");
  const todo = steps.filter((step) => step.group === "pending");

  return (
    <section className="flex min-w-0 flex-col rounded-md border bg-card shadow-raised" aria-labelledby={`${ids}-title`} data-testid="run-plan-steps">
      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1 border-b px-4 py-3">
        <h2 id={`${ids}-title`} className="text-[15px] leading-[22px] font-semibold">
          {t("title")}
        </h2>
        {counts ? (
          <span className="text-xs text-muted-foreground tabular-nums" data-testid="run-plan-steps-count">
            {t("count", { done: counts.done, total: counts.total, percent: percent(counts.done, counts.total) })}
          </span>
        ) : null}
      </div>
      <div className="min-w-0 px-2 py-2">
        {pending ? (
          <div className="flex flex-col gap-2 px-2 py-1" aria-hidden="true">
            <Skeleton className="h-5 w-full" />
            <Skeleton className="h-5 w-4/5" />
            <Skeleton className="h-5 w-3/5" />
          </div>
        ) : !view ? (
          <p className="px-2 text-sm text-muted-foreground" role="alert">
            {t("failed")}
          </p>
        ) : (
          <ol className="flex flex-col" data-steps>
            {now.map((step) => (
              <StepRow key={step.key} run={run} step={step} />
            ))}
            <StepFold run={run} steps={done} label={t("doneFold", { count: done.length })} testId="run-plan-steps-done-toggle" />
            <StepFold run={run} steps={todo} label={t("todoFold", { count: todo.length })} testId="run-plan-steps-todo-toggle" />
          </ol>
        )}
      </div>
    </section>
  );
}

/** The plan of a plan run as the hub holds it now, read again every 5 seconds while the run is active (one request). */
export function usePlanProgress(run: Run) {
  const active = isActiveState(run.state);
  const plan = useQuery({ ...planQuery(browserApi, run.project, run.plan_id), enabled: run.kind === "plan", refetchInterval: active ? LIVE_REFRESH_MS : false });
  const view = plan.data ? parsePlan(plan.data.body, plan.data.plan_id) : null;
  if (!view || view.steps.length === 0) return { pending: plan.isPending && run.kind === "plan", view: null, counts: null, current: null };
  const counts = countSteps(view.steps);
  const current = view.steps.find((step) => step.group === "in_progress") ?? view.steps.find((step) => step.group === "pending") ?? null;
  return { pending: false, view, counts, current };
}
