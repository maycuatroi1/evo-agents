"use client";

import { useFormatter, useTranslations } from "next-intl";

import { TONE_TEXT } from "@/components/status/status-badge";
import { percent, STEP_GROUPS, type StepCounts, type StepGroup } from "@/lib/plans";
import { cn } from "@/lib/utils";

import { stepLook, useStepStatusText } from "./status";

/** The state marks of web/DESIGN.md: green done, cobalt in progress, amber blocked, neutral the rest. */
const SEGMENT: Record<StepGroup, string> = {
  done: "bg-success-solid",
  in_progress: "bg-running",
  blocked: "bg-attention-solid",
  pending: "bg-transparent",
  other: "bg-neutral-solid",
};

/**
 * A bar of the steps by status. It is drawn for the eye only: the text beside it (done of total, and the legend)
 * says the same to everyone, so the bar is hidden from assistive technology.
 */
export function StepBar({ counts, className }: { counts: StepCounts; className?: string }) {
  const order: StepGroup[] = ["done", "in_progress", "blocked", "other", "pending"];
  return (
    <div
      aria-hidden="true"
      className={cn("flex h-2 w-full overflow-hidden rounded-full border border-input bg-muted", className)}
    >
      {counts.total > 0
        ? order
            .filter((group) => counts[group] > 0)
            .map((group) => (
              <span
                key={group}
                className={cn("h-full", SEGMENT[group])}
                style={{ width: `${(counts[group] / counts.total) * 100}%` }}
              />
            ))
        : null}
    </div>
  );
}

/** Done of total as words and a bar, for one row of the plans list. */
export function CompactProgress({ done, total }: { done: number; total: number }) {
  const t = useTranslations("plans.progress");
  const counts: StepCounts = { done, pending: total - done, in_progress: 0, blocked: 0, other: 0, total };
  return (
    <div className="flex min-w-36 flex-col gap-1.5">
      <span className="text-sm tabular-nums">
        <span className="font-medium">{t("doneOfTotal", { done, total })}</span>
        <span className="text-muted-foreground"> ({t("percent", { percent: percent(done, total) })})</span>
      </span>
      <StepBar counts={counts} className="max-w-48" />
    </div>
  );
}

/** The plan's progress: done of total in large type, the bar, and how many steps are in each status. */
export function PlanProgress({ counts }: { counts: StepCounts }) {
  const t = useTranslations("plans.progress");
  const label = useStepStatusText();
  const format = useFormatter();
  const shown = STEP_GROUPS.filter((group) => group !== "other" || counts.other > 0);
  return (
    <section aria-labelledby="plan-progress-title" className="flex flex-col gap-3 rounded-md border bg-card shadow-raised p-4">
      <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <h2 id="plan-progress-title" className="text-base font-medium">
          {t("title")}
        </h2>
        <p className="text-sm tabular-nums" data-testid="plan-progress">
          <span className="text-2xl font-semibold">{format.number(counts.done)}</span>
          <span className="text-muted-foreground">
            {" "}
            {t("ofTotal", { total: counts.total })} ({t("percent", { percent: percent(counts.done, counts.total) })})
          </span>
        </p>
      </div>
      <StepBar counts={counts} className="h-2.5" />
      <ul className="flex flex-wrap gap-x-5 gap-y-1.5 text-sm" aria-label={t("legend")}>
        {shown.map((group) => {
          const look = stepLook(group);
          return (
            <li key={group} className="flex items-center gap-1.5">
              <span className={cn("size-2.5 rounded-full border border-input", SEGMENT[group])} aria-hidden="true" />
              <look.icon className={cn("size-3.5", TONE_TEXT[look.tone])} aria-hidden="true" />
              <span>{group === "other" ? t("otherStatus") : label(group)}</span>
              <span className="font-mono text-xs font-medium tabular-nums" data-testid={`count-${group}`}>
                {counts[group]}
              </span>
            </li>
          );
        })}
      </ul>
    </section>
  );
}
