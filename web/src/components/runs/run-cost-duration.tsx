"use client";

import { useFormatter, useTranslations } from "next-intl";

import { durationParts } from "./model";
import type { Run } from "./queries";
import { modelLabel, runCost } from "./spend-model";
import { readRunUsage } from "./usage-model";

/**
 * What the run cost in dollars: what the hub's ledger keeps of it (spend-model.ts), so a run still going, waiting or
 * parked shows what it spent so far, else what its runtime reported with its end; a dash when there is neither. A cost
 * priced at list prices, in whole or in part, reads "≈" and says so in its title, with each model's share.
 */
export function CostCell({ run }: { run: Run }) {
  const format = useFormatter();
  const t = useTranslations("runs.columns");
  const spent = runCost(run);
  const cost = spent ? { amount: spent.amount, currency: "USD" } : readRunUsage(run.usage)?.cost;
  if (!cost) return <span className="text-fg-subtle">-</span>;
  const money = (amount: number) =>
    format.number(amount, { style: "currency", currency: cost.currency, maximumFractionDigits: amount > 0 && amount < 0.01 ? 4 : 2 });
  const amount = money(cost.amount);
  const estimated = spent !== null && spent.source !== "reported";
  const models = (run.spend?.models ?? []).map((model) => `${modelLabel(model.model)}: ${model.cost_usd === null ? "-" : money(model.cost_usd)}`);
  const title = [estimated ? t("costEstimated", { cost: amount }) : amount, ...models].join("\n");
  return (
    <span className="whitespace-nowrap font-mono text-xs" data-testid="run-cost" data-source={spent?.source ?? "usage"} title={title}>
      {estimated ? "≈" : ""}
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
