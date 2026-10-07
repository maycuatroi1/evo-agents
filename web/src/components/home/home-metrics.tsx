"use client";

import { Activity, CircleCheck, CircleX, Clock, MessageSquare } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";

import { type Metric, MetricStrip, QuietLine } from "@/components/data/metric-strip";

import { decisionsForYou, hasWork, lastFailed, oldestQueued, type Overview, runningRuns, utcDay } from "./model";
import { Relative } from "./parts";

/**
 * Home's metric strip: waiting on the visitor, running, queued, done in 7 days with its sparkline, failed in 7 days with
 * the lost ones; each cell names what is behind its number. With nothing in flight it gives way to the quiet line,
 * which still says the week's figures.
 */
export function HomeMetrics({ overview }: { overview: Overview }) {
  const t = useTranslations("home.metrics");
  const format = useFormatter();
  const { counts } = overview;
  const mine = decisionsForYou(overview);
  const running = runningRuns(overview);
  const queued = oldestQueued(overview);
  const failed = lastFailed(overview);
  const days = overview.done_by_day;
  const day = (value: string) => format.dateTime(utcDay(value), { month: "short", day: "numeric", timeZone: "UTC" });
  const series =
    days.length > 0
      ? {
          values: days.map((item) => item.done),
          label: t("doneSeries", {
            from: day(days[0].day),
            to: day(days[days.length - 1].day),
            values: format.list(days.map((item) => String(item.done)), { type: "unit" }),
          }),
        }
      : null;
  const firstRunning = running[0];
  const metrics: Metric[] = [
    {
      id: "waiting",
      label: t("waiting"),
      icon: MessageSquare,
      tone: "attention",
      inFlight: true,
      value: counts.waiting_on_you,
      meta: mine[0] ? t.rich("waitingMeta", { run: mine[0].run_id, time: () => <Relative value={mine[0].asked_at} /> }) : t("waitingNone"),
    },
    {
      id: "running",
      label: t("running"),
      icon: Activity,
      live: true,
      tone: "running",
      inFlight: true,
      value: counts.running,
      meta: firstRunning
        ? counts.running > 1
          ? t("runningMetaMore", { run: firstRunning.id, worker: firstRunning.worker ?? "-", more: counts.running - 1 })
          : t("runningMeta", { run: firstRunning.id, worker: firstRunning.worker ?? "-" })
        : t("runningNone"),
    },
    {
      id: "queued",
      label: t("queued"),
      icon: Clock,
      inFlight: true,
      value: counts.queued,
      meta: queued ? t.rich("queuedMeta", { run: queued.id, time: () => <Relative value={queued.queued_at} /> }) : t("queuedNone"),
    },
    { id: "done", label: t("done"), icon: CircleCheck, value: counts.done_7d, series },
    {
      id: "failed",
      label: t("failed"),
      icon: CircleX,
      value: counts.failed_7d,
      extra: counts.lost_7d > 0 ? t("failedLost", { count: counts.lost_7d }) : null,
      meta: failed
        ? failed.error
          ? t("failedMeta", { run: failed.id, error: failed.error })
          : t("failedMetaPlain", { run: failed.id, project: failed.project })
        : t("failedNone"),
    },
  ];
  return (
    <MetricStrip
      label={t("label")}
      testId="home-metrics"
      metrics={metrics}
      quiet={
        hasWork(overview) ? undefined : (
          <QuietLine icon={CircleCheck} title={t("quietTitle")} testId="home-quiet">
            {t("quietText")}
            {counts.done_7d + counts.failed_7d + counts.lost_7d > 0 ? (
              <> {t("quietWeek", { done: counts.done_7d, failed: counts.failed_7d, lost: counts.lost_7d })}</>
            ) : null}
          </QuietLine>
        )
      }
    />
  );
}
