"use client";

import { useFormatter, useTranslations } from "next-intl";

import { TONE_TEXT } from "@/components/status/status-badge";
import { STRIP_STATUSES, type StripStatus, stripStatus } from "@/lib/plan-list";
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

/** The marks of the step strip; descoped is hatched in `neutral-solid`, so it reads apart from to do without colour. */
const STRIP: Record<StripStatus, string> = {
  done: "bg-success-solid",
  in_progress: "bg-running",
  blocked: "bg-danger-solid",
  pending: "border border-border-strong bg-card",
  descoped: "border border-neutral-solid bg-[repeating-linear-gradient(135deg,var(--neutral-solid)_0_1px,transparent_1px_3px)]",
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

/**
 * The step strip of a row of the plans list: one mark per step, in the plan's order, so the eye reads where the plan
 * stands and where it is stuck. Green done, cobalt in progress, red blocked, an empty box to do, hatched descoped and
 * neutral a status the schema lacks. The marks share the width; the strip is an image named by its counts.
 */
export function StepStrip({
  steps,
  className,
}: {
  steps: readonly { key: string; status: string | null }[];
  className?: string;
}) {
  const t = useTranslations("plans.strip");
  if (steps.length === 0) return null;
  const marks = steps.map((step) => ({ key: step.key, status: stripStatus(step.status) }));
  const counts = STRIP_STATUSES.map((status) => [status, marks.filter((mark) => mark.status === status).length] as const);
  const label = counts
    .filter(([, count]) => count > 0)
    .map(([status, count]) => t(status, { count }))
    .join(", ");
  return (
    <div
      role="img"
      aria-label={t("label", { counts: label })}
      className={cn("flex h-2 w-full min-w-0 gap-px", className)}
      data-testid="step-strip"
    >
      {marks.map((mark, index) => (
        <span
          key={`${mark.key}-${index}`}
          className={cn("h-full min-w-px flex-1 rounded-[1px]", STRIP[mark.status])}
          data-status={mark.status}
          data-step={mark.key}
        />
      ))}
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
        <h2 id="plan-progress-title" className="text-[15px] leading-[22px] font-semibold">
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
