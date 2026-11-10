"use client";

import { CircleDot, CircleX, Coins, Hand, Timer } from "lucide-react";
import { useTranslations } from "next-intl";

import { type Metric, MetricStrip } from "@/components/data/metric-strip";
import { Skeleton } from "@/components/ui/skeleton";
import type { WorkSummary } from "@/lib/systems-work";

import { useUsd, useWorkDuration } from "./work-status";

/**
 * The row of numbers over Observe's work list: the work of the last 24 hours that is open, needs a person, failed,
 * the median time a work took to close and what it all cost, in one container. A person needed is the one number in
 * `attention`; the work left out because the reader may not read it is said under the first.
 */
export function WorkSummaryStrip({ summary, failed }: { summary: WorkSummary | undefined; failed: boolean }) {
  const t = useTranslations("systems.observe.work.summary");
  const duration = useWorkDuration();
  const usd = useUsd();
  if (!summary) {
    return failed ? (
      <p className="rounded-md border bg-card px-4 py-3 text-[13px] text-danger" data-testid="system-work-summary-failed">
        {t("failedToLoad")}
      </p>
    ) : (
      <Skeleton className="h-[78px] w-full" />
    );
  }
  const metrics: Metric[] = [
    { id: "work-open", label: t("open"), icon: CircleDot, value: summary.open, meta: summary.withheld ? t("withheld", { count: summary.withheld }) : null },
    { id: "work-needs-person", label: t("needsPerson"), icon: Hand, value: summary.needs_person, tone: "attention" },
    { id: "work-failed", label: t("failed"), icon: CircleX, value: summary.failed },
    {
      id: "work-close-time",
      label: t("closeTime"),
      icon: Timer,
      value: summary.closed_p50_seconds ?? 0,
      display: summary.closed_p50_seconds === null || summary.closed_p50_seconds === undefined ? "-" : duration(summary.closed_p50_seconds),
    },
    { id: "work-cost", label: t("cost"), icon: Coins, value: summary.usd, display: usd(summary.usd) },
  ];
  return <MetricStrip label={t("label")} metrics={metrics} testId="system-work-summary" />;
}
