"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { useId } from "react";

import { stepHref } from "@/components/plans/links";
import { StepStatusBadge } from "@/components/plans/status";
import { Skeleton } from "@/components/ui/skeleton";
import { browserApi } from "@/lib/api/browser";
import { planQuery } from "@/lib/plan-queries";
import { countSteps, parsePlan, percent, stepLabel } from "@/lib/plans";
import { cn } from "@/lib/utils";

import { isActiveState, LIVE_REFRESH_MS, type Run } from "./queries";

/**
 * The steps of a plan run's plan, as the hub holds the plan now: each with its status, the step in progress marked,
 * and the count done. The run reports each step as it goes, so the list is read again every 5 seconds while the run
 * is active.
 */
export function RunPlanSteps({ run }: { run: Run }) {
  const t = useTranslations("runs.detail.planSteps");
  const ids = useId();
  const active = isActiveState(run.state);
  const plan = useQuery({ ...planQuery(browserApi, run.project, run.plan_id), refetchInterval: active ? LIVE_REFRESH_MS : false });
  const view = plan.data ? parsePlan(plan.data.body, plan.data.plan_id) : null;
  const counts = view ? countSteps(view.steps) : null;

  return (
    <section className="flex min-w-0 flex-col rounded-md border bg-card shadow-raised" aria-labelledby={`${ids}-title`} data-testid="run-plan-steps">
      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1 border-b px-4 py-3">
        <h2 id={`${ids}-title`} className="text-base font-medium">
          {t("title")}
        </h2>
        {counts ? (
          <span className="text-xs text-muted-foreground tabular-nums" data-testid="run-plan-steps-count">
            {t("count", { done: counts.done, total: counts.total, percent: percent(counts.done, counts.total) })}
          </span>
        ) : null}
      </div>
      <div className="min-w-0 px-4 py-3">
        {plan.isPending ? (
          <div className="flex flex-col gap-2" aria-hidden="true">
            <Skeleton className="h-5 w-full" />
            <Skeleton className="h-5 w-4/5" />
            <Skeleton className="h-5 w-3/5" />
          </div>
        ) : plan.isError || !view ? (
          <p className="text-sm text-muted-foreground" role="alert">
            {t("failed")}
          </p>
        ) : view.steps.length === 0 ? (
          <p className="text-sm text-muted-foreground">{t("none")}</p>
        ) : (
          <ol className="flex flex-col gap-1.5">
            {view.steps.map((step) => (
              <li
                key={step.key}
                className={cn(
                  "flex min-w-0 flex-col gap-1 rounded-md px-2 py-1.5",
                  step.group === "in_progress" ? "bg-accent" : "hover:bg-muted/50",
                )}
                aria-current={step.group === "in_progress" ? "step" : undefined}
                data-testid="run-plan-step"
                data-status={step.group}
              >
                <span className="flex min-w-0 items-start gap-2 text-sm">
                  <span className="mt-0.5 w-6 shrink-0 text-right font-mono text-xs text-muted-foreground tabular-nums">{step.key}</span>
                  <Link
                    href={stepHref(run.project, run.plan_id, step.key)}
                    className="min-w-0 flex-1 text-brand underline-offset-4 [overflow-wrap:anywhere] hover:underline"
                  >
                    {stepLabel(step) || t("untitled")}
                  </Link>
                </span>
                <span className="flex flex-wrap items-center gap-1.5 pl-8">
                  <StepStatusBadge group={step.group} raw={step.rawStatus} />
                  {step.repo ? <span className="font-mono text-xs text-muted-foreground [overflow-wrap:anywhere]">{step.repo}</span> : null}
                </span>
              </li>
            ))}
          </ol>
        )}
        <p className="mt-3 text-xs text-pretty text-muted-foreground">{t("hint")}</p>
      </div>
    </section>
  );
}
