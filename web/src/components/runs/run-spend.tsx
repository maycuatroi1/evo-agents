"use client";

import { useFormatter, useTranslations } from "next-intl";
import { useId } from "react";

import type { Run } from "./queries";
import { type CostSource, costSource, modelLabel } from "./spend-model";
import { useCost } from "./usage-meter";

/**
 * What the run spent, by model, in the run's side column under its usage: the cost and tokens the hub's ledger keeps
 * of it (spend-model.ts), each model a row with its tokens apart (input, cache read, cache write, output) and its cost,
 * said as reported, estimated at list prices, or both. No card before the run's first usage report.
 */
export function RunSpend({ run }: { run: Pick<Run, "spend" | "state"> }) {
  const t = useTranslations("runs.detail.spend");
  const format = useFormatter();
  const costText = useCost();
  const id = useId();
  const spend = run.spend;
  if (!spend) return null;
  const money = (amount: number | null) => (amount === null ? null : costText({ amount, currency: "USD" }));
  const source = (value: CostSource | null) => (value ? t(`source.${value}`) : t("source.none"));
  return (
    <section className="flex min-w-0 flex-col rounded-md border bg-card shadow-raised" aria-labelledby={id} data-testid="run-spend">
      <div className="flex items-center gap-2 border-b px-4 py-3">
        <h2 id={id} className="text-[15px] leading-[22px] font-semibold">
          {t("title")}
        </h2>
        <span className="ml-auto truncate text-xs text-fg-subtle">{t("sessions", { count: spend.sessions })}</span>
      </div>
      <div className="flex min-w-0 flex-col gap-3 px-4 py-3">
        <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
          <b className="text-xl leading-[26px] font-semibold tabular-nums" data-testid="run-spend-cost">
            {money(spend.cost_usd) ?? t("noCost")}
          </b>
          <span className="text-sm text-fg-subtle">{source(costSource(spend))}</span>
          <span className="ml-auto text-sm text-muted-foreground tabular-nums">
            {t("tokens", { count: format.number(spend.tokens.total) })}
          </span>
        </div>
        {spend.reported_usd !== null && spend.estimated_usd !== null && spend.estimated_usd > 0 ? (
          <p className="text-xs text-fg-subtle" data-testid="run-spend-split">
            {t("split", { reported: money(spend.reported_usd) ?? "", estimated: money(spend.estimated_usd) ?? "" })}
          </p>
        ) : null}
        <ul className="flex flex-col divide-y text-[13px] leading-[18px]" data-testid="run-spend-models">
          {spend.models.map((model) => (
            <li key={model.model} className="flex flex-col gap-0.5 py-2 first:pt-0 last:pb-0" data-model={model.model}>
              <div className="flex min-w-0 items-baseline gap-2">
                <span className="min-w-0 truncate font-mono text-xs text-foreground">{modelLabel(model.model)}</span>
                <span className="ml-auto text-right text-foreground tabular-nums">{money(model.cost_usd) ?? t("noCost")}</span>
              </div>
              <div className="flex flex-wrap gap-x-3 text-xs text-fg-subtle tabular-nums">
                <span>{t("parts.input", { count: format.number(model.tokens.input) })}</span>
                <span>{t("parts.cacheRead", { count: format.number(model.tokens.cache_read) })}</span>
                <span>{t("parts.cacheWrite", { count: format.number(model.tokens.cache_write) })}</span>
                <span>{t("parts.output", { count: format.number(model.tokens.output) })}</span>
                {costSource(model) && costSource(model) !== "reported" ? <span>{source(costSource(model))}</span> : null}
              </div>
            </li>
          ))}
        </ul>
        <p className="text-xs text-pretty text-fg-subtle">{t(run.state === "running" || run.state === "waiting" || run.state === "parked" ? "noteLive" : "note")}</p>
      </div>
    </section>
  );
}
