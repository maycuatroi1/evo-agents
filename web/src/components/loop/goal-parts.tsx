"use client";

import { CircleCheck, CircleDashed, CircleDotDashed } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";

import { Meter } from "@/components/data/meter";
import { StatusPill } from "@/components/status/status-badge";
import type { GoalMetric, LoopGoal } from "@/lib/loop-queries";
import { cn } from "@/lib/utils";

import { ItemStateBadge } from "./badges";
import { countOf, type ItemCounts, metricParts, progressOf } from "./model";

/** How a goal's metric reads: "checkout.conversion, mean over 30 days, >= 0.6", with its tenant when it names one. */
export function useMetricText(): (metric: GoalMetric) => string {
  const t = useTranslations("loop.metric");
  const format = useFormatter();
  return (metric) => {
    const parts = metricParts(metric);
    const target = format.number(parts.target, { maximumFractionDigits: 4 });
    const text = t("text", { key: parts.key, aggregate: parts.aggregate, days: parts.days, comparator: parts.comparator, target });
    return parts.tenant ? t("tenant", { text, tenant: parts.tenant }) : text;
  };
}

/** Whether a goal meets its target now: reached, not yet, or no signal in its window to tell. */
export function ReachedBadge({ figure }: { figure: LoopGoal | null }) {
  const t = useTranslations("loop.metric");
  if (!figure || figure.reached === null) {
    return <StatusPill look={{ tone: "outline", icon: CircleDashed }} label={t("noValue")} data-testid="goal-reached" data-reached="unknown" />;
  }
  return figure.reached ? (
    <StatusPill look={{ tone: "success", icon: CircleCheck }} label={t("reached")} data-testid="goal-reached" data-reached="true" />
  ) : (
    <StatusPill look={{ tone: "neutral", icon: CircleDotDashed }} label={t("notReached")} data-testid="goal-reached" data-reached="false" />
  );
}

/**
 * A goal's value now against its target: the figure, the target and a meter of how near it is, with whether it is
 * reached in words, so the meter is never the only cue. `large` is the goal's own page.
 */
export function GoalProgress({ metric, figure, large = false }: { metric: GoalMetric | null; figure: LoopGoal | null; large?: boolean }) {
  const t = useTranslations("loop.metric");
  const format = useFormatter();
  if (!metric) return <span className="text-[13px] text-fg-subtle">{t("none")}</span>;
  const progress = progressOf(metric, figure);
  const value = figure?.value ?? null;
  const shown = value === null ? t("noSignal") : format.number(value, { maximumFractionDigits: 4 });
  const target = `${metric.comparator} ${format.number(metric.target, { maximumFractionDigits: 4 })}`;
  return (
    <div className={cn("flex min-w-0 flex-col gap-1.5", large ? "w-full" : "w-40 max-w-full")} data-testid="goal-progress">
      <div className="flex min-w-0 flex-wrap items-baseline gap-x-2">
        <span
          className={cn("font-semibold tabular-nums", large ? "text-2xl leading-[30px]" : "text-sm", value === null ? "text-fg-subtle" : "text-foreground")}
          data-testid="goal-value"
        >
          {shown}
        </span>
        <span className="text-xs text-muted-foreground tabular-nums">{t("target", { target })}</span>
      </div>
      {progress !== null ? (
        <Meter value={progress} label={t("meter")} valueText={t("meterText", { value: shown, target })} className={large ? "h-2.5" : undefined} />
      ) : null}
    </div>
  );
}

const SHOWN_STATES = ["running", "waiting_human", "queued", "watching"] as const;

/** A goal's unfinished work items by state, as small pills with their counts; "None open" when the Loop does nothing for it. */
export function OpenItems({ items }: { items: ItemCounts | null }) {
  const t = useTranslations("loop.goals");
  const counted = useTranslations("loop.openState");
  const open = SHOWN_STATES.filter((value) => countOf(items, value) > 0);
  if (!items || open.length === 0) return <span className="text-[13px] text-fg-subtle">{t("noneOpen")}</span>;
  return (
    <ul className="flex flex-nowrap gap-1.5" aria-label={t("openLabel")} data-testid="goal-open-items">
      {open.map((value) => (
        <li key={value}>
          <ItemStateBadge state={value} label={counted(value, { count: countOf(items, value) })} data-count={countOf(items, value)} />
        </li>
      ))}
    </ul>
  );
}
