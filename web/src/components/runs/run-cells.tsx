"use client";

import { useFormatter, useTranslations } from "next-intl";

import { useNow } from "@/hooks/use-now";
import { cn } from "@/lib/utils";

import { durationParts, runTiming } from "./model";
import { isActiveState, type Run } from "./queries";
import { readRunUsage } from "./usage-model";

/** Cells of the runs table (runs-table.tsx) that read a run's usage and its clock: its cost, start and duration. */

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

export function Timing({ run, part }: { run: Run; part: "started" | "duration" }) {
  const t = useTranslations("runs");
  const format = useFormatter();
  const duration = useDuration();
  const now = useNow(isActiveState(run.state));
  const timing = runTiming(run, now);
  if (part === "started") {
    const at = timing.startedAt ?? run.queued_at;
    const date = new Date(at);
    return (
      <time
        dateTime={at}
        title={format.dateTime(date, { dateStyle: "medium", timeStyle: "medium" })}
        className={cn("whitespace-nowrap", !timing.startedAt && "text-fg-subtle")}
      >
        {timing.startedAt ? null : <span className="sr-only">{t("queuedAt")} </span>}
        {format.dateTime(date, { dateStyle: "short", timeStyle: "short" })}
      </time>
    );
  }
  if (timing.durationMs === null) return <span className="text-fg-subtle">-</span>;
  return (
    <span className={cn("whitespace-nowrap", timing.waiting && "text-fg-subtle")} data-testid="run-duration">
      {timing.waiting ? t("waiting", { duration: duration(timing.durationMs) }) : duration(timing.durationMs)}
    </span>
  );
}
