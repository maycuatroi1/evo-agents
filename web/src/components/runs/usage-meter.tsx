"use client";

import { useFormatter, useTranslations } from "next-intl";
import { useId, useMemo } from "react";

import { cn } from "@/lib/utils";

import { isActiveState, type Run } from "./queries";
import { type Cost, runUsage, shares, totalTokens, USAGE_PARTS, type UsagePart } from "./usage-model";

/**
 * The kit's UsageMeter, in the run's side column: the tokens the runtime reported, as one total, a bar of cache read,
 * input, output and reasoning (`chart-5`, `chart-1`, `chart-3`, `chart-4`; each part keeps 3 px so a small share still
 * shows) and a row per part with its share; cache writes are said under it. The cost is the one the runtime reported,
 * or "not reported": the page never prices tokens itself. While the run goes on, the figures add up its usage reports
 * so far (usage-model.ts says how each runtime reports); once the worker reported the run's end, they are the run's.
 */

const SWATCH: Record<UsagePart, string> = {
  cacheRead: "bg-chart-5",
  input: "bg-chart-1",
  output: "bg-chart-3",
  reasoning: "bg-chart-4",
};

function useCost() {
  const format = useFormatter();
  return (cost: Cost) => {
    try {
      return format.number(cost.amount, {
        style: "currency",
        currency: cost.currency,
        minimumFractionDigits: 2,
        maximumFractionDigits: cost.amount > 0 && cost.amount < 0.01 ? 4 : 2,
      });
    } catch {
      return `${format.number(cost.amount, { maximumFractionDigits: 4 })} ${cost.currency}`;
    }
  };
}

export function UsageMeter({ run, events }: { run: Pick<Run, "usage" | "state" | "runtime">; events: Parameters<typeof runUsage>[1] }) {
  const t = useTranslations("runs.detail.usage");
  const tRuntime = useTranslations("runs.runtime");
  const format = useFormatter();
  const costText = useCost();
  const id = useId();
  const reported = run.usage;
  const usage = useMemo(() => runUsage({ usage: reported }, events), [reported, events]);
  const active = isActiveState(run.state);
  const figures = usage?.figures ?? null;
  const total = figures ? totalTokens(figures) : 0;
  const share = figures ? shares(figures) : null;
  // A part that is there but rounds to 0.0 % says so, rather than 0 %.
  const percent = (part: UsagePart) =>
    figures && share && figures[part] > 0 && share[part] < 0.1 ? t("tiny") : format.number(share?.[part] ?? 0, { maximumFractionDigits: 1 });

  return (
    <section className="flex min-w-0 flex-col rounded-md border bg-card shadow-raised" aria-labelledby={id} data-testid="run-usage" data-source={usage?.source ?? "none"}>
      <div className="flex items-center gap-2 border-b px-4 py-3">
        <h2 id={id} className="text-[15px] leading-[22px] font-semibold">
          {t("title")}
        </h2>
        <span className="ml-auto truncate text-xs text-fg-subtle">
          {usage?.source === "events" && active ? t("soFar") : run.runtime !== "any" ? tRuntime(run.runtime) : null}
        </span>
      </div>
      <div className="flex min-w-0 flex-col gap-3 px-4 py-3">
        {!figures || !share ? (
          <p className="text-sm text-pretty text-muted-foreground" data-testid="run-usage-empty">
            {active ? t("none") : t("noneEnded")}
          </p>
        ) : (
          <>
            <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
              <b className="text-xl leading-[26px] font-semibold tabular-nums" data-testid="run-usage-total">
                {format.number(total)}
              </b>
              <span className="text-sm text-muted-foreground">{total === 1 ? t("token") : t("tokens")}</span>
              <span className="ml-auto text-sm tabular-nums" data-testid="run-usage-cost">
                {figures.cost ? (
                  <>
                    <span className="text-foreground">{costText(figures.cost)}</span>{" "}
                    <span className="text-fg-subtle">{t("asReported")}</span>
                  </>
                ) : (
                  <span className="text-fg-subtle">{t("costNotReported")}</span>
                )}
              </span>
            </div>
            <div
              role="img"
              aria-label={t("bar", {
                cacheRead: percent("cacheRead"),
                input: percent("input"),
                output: percent("output"),
                reasoning: percent("reasoning"),
              })}
              className="flex h-2 gap-0.5 overflow-hidden rounded-full bg-muted"
              data-testid="run-usage-bar"
            >
              {USAGE_PARTS.map((part) =>
                figures[part] > 0 ? (
                  <span key={part} className={cn("block h-full min-w-[3px]", SWATCH[part])} style={{ width: `${share[part]}%` }} data-part={part} />
                ) : null,
              )}
            </div>
            <dl className="grid grid-cols-[minmax(0,1fr)_auto_auto] gap-x-4 gap-y-1.5 text-[13px] leading-[18px]" data-testid="run-usage-rows">
              {USAGE_PARTS.map((part) => (
                <div key={part} className="contents" data-part={part}>
                  <dt className="flex min-w-0 items-center gap-2 text-muted-foreground">
                    <i aria-hidden="true" className={cn("size-2 shrink-0 rounded-[2px]", SWATCH[part])} />
                    {t(`parts.${part}`)}
                  </dt>
                  <dd className="text-right text-foreground tabular-nums">{format.number(figures[part])}</dd>
                  <dd className="min-w-11 text-right text-fg-subtle tabular-nums">
                    {t("share", { value: percent(part) })}
                  </dd>
                </div>
              ))}
            </dl>
            <p className="text-xs text-pretty text-fg-subtle" data-testid="run-usage-note">
              {t("cacheWrite", { count: format.number(figures.cacheWrite) })} {figures.cost ? t("costNote") : t("noCostNote")}
            </p>
          </>
        )}
      </div>
    </section>
  );
}
