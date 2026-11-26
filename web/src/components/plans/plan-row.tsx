"use client";

import { Ban } from "lucide-react";
import Link from "next/link";
import { useFormatter, useTranslations } from "next-intl";
import { memo, type ReactNode } from "react";

import { ProblemChips } from "@/components/data/problem-chips";
import { calendarDay, changeKind, currentPosition, planFinished, planName } from "@/lib/plan-list";
import type { PlanSummary } from "@/lib/plans";
import { cn } from "@/lib/utils";

import { planHref } from "./links";
import { StepStrip } from "./progress";

/** When the plan last changed: the time today, Yesterday, the weekday this week, then the date; the full time on hover. */
function LastChange({ at, today, timeZone }: { at: string; today: string | null; timeZone: string }) {
  const t = useTranslations("plans.list");
  const format = useFormatter();
  const date = new Date(at);
  const full = format.dateTime(date, { dateStyle: "medium", timeStyle: "short" });
  let text: string;
  // On the server and during hydration the day is not known yet: the date, which reads the same everywhere.
  if (today === null) text = format.dateTime(date, { dateStyle: "medium" });
  else {
    const kind = changeKind(calendarDay(date, timeZone), today);
    if (kind === "time") text = format.dateTime(date, { timeStyle: "short" });
    else if (kind === "yesterday") text = t("yesterday");
    else if (kind === "weekday") text = format.dateTime(date, { weekday: "long" });
    else if (kind === "date") text = format.dateTime(date, { day: "numeric", month: "short" });
    else text = format.dateTime(date, { dateStyle: "medium" });
  }
  return (
    <time dateTime={at} title={full} className="whitespace-nowrap tabular-nums" data-testid="plan-changed">
      {text}
    </time>
  );
}

/** "Step 7 of 8: title" for the first step not done, in `danger` when it is blocked; else that every step is done. */
function CurrentStep({ plan }: { plan: PlanSummary }) {
  const t = useTranslations("plans.list");
  const step = plan.current_step ?? null;
  const base = "min-w-0 truncate text-[13px] leading-[18px]";
  if (!step) {
    return (
      <span className={cn(base, "text-muted-foreground")} data-testid="plan-current-step" data-status="none">
        {plan.steps_total > 0 ? t("allDone", { total: plan.steps_total }) : t("noSteps")}
      </span>
    );
  }
  const position = currentPosition(plan);
  const values = { total: plan.steps_total, title: step.title ?? step.key };
  const text = position === null ? t("currentStepKey", { key: step.key, ...values }) : t("currentStep", { position, ...values });
  const blocked = step.status === "blocked";
  return (
    <span
      className={cn(base, "flex items-center gap-1", blocked ? "font-medium text-danger" : "text-muted-foreground")}
      title={text}
      data-testid="plan-current-step"
      data-status={step.status ?? "pending"}
      data-step={step.key}
    >
      {blocked ? <Ban className="size-3.5 shrink-0" aria-hidden="true" /> : null}
      <span className="min-w-0 truncate">
        {blocked ? <span className="sr-only">{t("blocked")} </span> : null}
        {text}
      </span>
    </span>
  );
}

/**
 * One plan of the list (web/DESIGN.md, Plans): its name and slug with the mark of its active plan run, its current
 * step, the step strip with the steps done of the total, then its problems, who changed it last and when. The name's
 * link covers the row; the problem chips and the run's mark sit above that cover and open their own pages. A plan
 * whose steps are all done or descoped is written faint. The row holds no button: a plan changes from the CLI.
 */
export const PlanRow = memo(function PlanRow({
  project,
  plan,
  activity,
  today,
  timeZone,
}: {
  project: string;
  plan: PlanSummary;
  /** The badge of the plan's active plan run or system trace, from the plans list; null for none. */
  activity: ReactNode;
  today: string | null;
  timeZone: string;
}) {
  const t = useTranslations("plans");
  const name = planName(plan);
  const finished = planFinished(plan);
  return (
    <li
      className={cn(
        "relative grid min-h-14 gap-x-6 gap-y-2 px-4 py-3 transition-colors hover:bg-accent",
        "md:grid-cols-[minmax(0,1fr)_minmax(8rem,13rem)_minmax(0,15rem)] md:items-center",
        "[&_:is(a,button):not([data-row-link])]:relative [&_:is(a,button):not([data-row-link])]:z-[1]",
        finished && "text-muted-foreground",
      )}
      data-testid="plan-row"
      data-plan-id={plan.plan_id}
      data-finished={finished}
    >
      <div className="flex min-w-0 flex-col gap-0.5">
        <div className="flex min-w-0 items-center gap-2">
          <Link
            href={planHref(project, plan.plan_id)}
            title={name}
            data-row-link=""
            data-testid="plan-link"
            className={cn(
              "min-w-0 truncate text-sm leading-5 font-medium transition-colors hover:text-brand",
              finished ? "text-muted-foreground" : "text-foreground",
              "after:absolute after:inset-0 after:content-[''] focus-visible:outline-none",
              "focus-visible:after:rounded-xs focus-visible:after:outline-2 focus-visible:after:-outline-offset-2 focus-visible:after:outline-ring",
            )}
          >
            {name}
          </Link>
          {activity}
        </div>
        {name !== plan.plan_id ? (
          <span className="min-w-0 truncate font-mono text-xs leading-4 text-fg-subtle" data-testid="plan-slug">
            {plan.plan_id}
          </span>
        ) : null}
        <CurrentStep plan={plan} />
      </div>
      <div className="flex min-w-0 items-center gap-2">
        <StepStrip steps={plan.steps ?? []} className="flex-1" />
        <span className="shrink-0 text-xs leading-4 tabular-nums" data-testid="plan-steps-count">
          <span aria-hidden="true">{t("list.stepsCount", { done: plan.steps_done, total: plan.steps_total })}</span>
          <span className="sr-only">{t("progress.doneOfTotal", { done: plan.steps_done, total: plan.steps_total })}</span>
        </span>
      </div>
      <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1 text-xs leading-4 text-muted-foreground md:justify-end">
        <span className="min-w-0 truncate font-mono" title={plan.updated_by} data-testid="plan-owner">
          <span className="sr-only">{t("list.changedBy")} </span>
          {plan.updated_by}
        </span>
        <LastChange at={plan.updated_at} today={today} timeZone={timeZone} />
        {/* First from 768 px, as the columns read; last on a phone, so a tap on the line's start opens the plan. */}
        <ProblemChips project={project} codes={plan.problems ?? []} className="md:order-first" />
      </div>
    </li>
  );
});
