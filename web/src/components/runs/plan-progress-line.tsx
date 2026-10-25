import { useTranslations } from "next-intl";

import { cn } from "@/lib/utils";

import type { Run } from "./queries";

/** A plan run's progress: a bar of how much of its plan is done, then the step it is at, with that step's status. */
export function PlanProgressLine({ run }: { run: Run }) {
  const t = useTranslations("runs.progress");
  const progress = run.plan_progress;
  if (!progress) return null;
  const step = progress.current_step ? t("at", { step: progress.current_step, status: progress.current_status ?? "" }) : t("complete");
  return (
    <span className="flex w-56 max-w-full flex-col gap-1 text-xs text-fg-subtle" data-testid="run-progress">
      <span className="flex items-center gap-2 tabular-nums">
        <span
          role="progressbar"
          aria-valuemin={0}
          aria-valuemax={progress.total}
          aria-valuenow={progress.done}
          aria-label={t("label")}
          className="h-1.5 min-w-0 flex-1 overflow-hidden rounded-full bg-muted"
        >
          <span
            className={cn("block h-full rounded-full", progress.current_step ? "bg-running" : "bg-success-solid")}
            style={{ width: `${Math.min(100, Math.max(0, progress.percent))}%` }}
          />
        </span>
        <span className="shrink-0" data-testid="run-progress-count">
          {t("count", { done: progress.done, total: progress.total })}
        </span>
      </span>
      <span className="truncate" title={step} data-testid="run-progress-step">
        {step}
      </span>
    </span>
  );
}
