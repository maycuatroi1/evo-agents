"use client";

import { useFormatter, useTranslations } from "next-intl";

import { durationParts } from "./model";
import type { Run } from "./queries";
import { readRunUsage } from "./usage-model";

/** What the run cost in dollars, as its runtime reported it; a dash when it reported none. */
export function CostCell({ run }: { run: Run }) {
  const format = useFormatter();
  const cost = readRunUsage(run.usage)?.cost;
  if (!cost) return <span className="text-fg-subtle">-</span>;
  const amount = format.number(cost.amount, {
    style: "currency",
    currency: cost.currency,
    maximumFractionDigits: cost.amount > 0 && cost.amount < 0.01 ? 4 : 2,
  });
  return (
    <span className="whitespace-nowrap font-mono text-xs" data-testid="run-cost">
      {amount}
    </span>
  );
}

export function useDuration() {
  const t = useTranslations("runs.duration");
  return (ms: number) => {
    const { hours, minutes } = durationParts(ms);
    if (hours > 0) return t("hours", { hours, minutes });
    return minutes > 0 ? t("minutes", { minutes }) : t("lessThanMinute");
  };
}
