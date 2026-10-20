"use client";

import { useQueries } from "@tanstack/react-query";
import { CircleDollarSign, Database, Layers, Timer } from "lucide-react";
import { usePathname, useSearchParams } from "next/navigation";
import { useTranslations } from "next-intl";

import { type Metric, MetricStrip } from "@/components/data/metric-strip";
import { Segmented } from "@/components/data/segmented";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { ApiErrorState, EmptyState } from "@/components/states/states";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";

import { CuratorHeader, CuratorTabs } from "./curator-header";
import { CuratorHeadSkeleton, useCuratorStatus } from "./curator-page";
import { figuresQuery } from "./insight-queries";
import { useInsightFormat } from "./insight-format";
import { type CuratorStatus, nightsQuery } from "./queries";
import {
  countedNights,
  DEFAULT_RANGE,
  readRuntimeRange,
  RUNTIME_RANGES,
  runtimeNight,
  type RuntimeNight,
  type RuntimeRange,
  runtimeTrends,
  slowestHook,
} from "./runtime-model";
import { RuntimeDetails } from "./runtime-tables";
import { TrendCard, TrendSkeleton } from "./trend-card";
import { series } from "./trend-model";

function useRange(): [RuntimeRange, (next: RuntimeRange) => void] {
  const params = useSearchParams();
  const pathname = usePathname();
  const range = readRuntimeRange(params.get("nights"));
  const set = (next: RuntimeRange) => window.history.replaceState(null, "", next === DEFAULT_RANGE ? pathname : `${pathname}?nights=${next}`);
  return [range, set];
}

function RuntimeStrip({ night }: { night: RuntimeNight }) {
  const t = useTranslations("curator.runtime.strip");
  const format = useInsightFormat();
  const hook = slowestHook(night);
  const metrics: Metric[] = [
    {
      id: "fixed",
      label: t("fixed"),
      icon: Layers,
      value: night.fixedPerSession ?? 0,
      display: night.fixedPerSession === null ? "-" : format.count(night.fixedPerSession),
      meta: night.harnessSessions > 0 ? t("fixedMeta", { sessions: night.harnessSessions }) : t("fixedNone"),
    },
    {
      id: "cache",
      label: t("cache"),
      icon: Database,
      value: night.sessions.cacheRead ?? 0,
      display: night.sessions.cacheRead === null ? "-" : format.percent(night.sessions.cacheRead),
      meta: night.runs.cacheRead !== null ? t("cacheMeta", { runs: format.percent(night.runs.cacheRead) }) : t("cacheNoRuns"),
    },
    {
      id: "cost",
      label: t("cost"),
      icon: CircleDollarSign,
      value: night.sessions.costPer ?? 0,
      display: night.sessions.costPer === null ? "-" : format.money(night.sessions.costPer),
      meta: t("costMeta", {
        run: night.runs.costPer === null ? "-" : format.money(night.runs.costPer),
        pr: night.merged.costPer === null ? "-" : format.money(night.merged.costPer),
      }),
    },
    {
      id: "hook",
      label: t("hook"),
      icon: Timer,
      value: hook?.p95 ?? 0,
      display: hook?.p95 == null ? "-" : format.ms(hook.p95),
      meta: hook ? hook.name : t("hookNone"),
    },
  ];
  return <MetricStrip label={t("label")} metrics={metrics} testId="runtime-strip" />;
}

function Trends({ nights, charsPerToken }: { nights: RuntimeNight[]; charsPerToken: number }) {
  const t = useTranslations("curator.runtime.trend");
  const format = useInsightFormat();
  const trends = runtimeTrends(nights, (night) => ({ tick: format.shortDay(night), title: format.day(night) }));
  return (
    <div className="grid min-w-0 gap-5 lg:grid-cols-2" data-testid="runtime-trends">
      <TrendCard
        title={t("contextTitle")}
        headline={t("contextHeadline", { chars: charsPerToken })}
        empty={t("contextEmpty")}
        kind="bars"
        rows={trends.context}
        series={series([
          { key: "always_loaded", label: t("source.always_loaded") },
          { key: "mcp_instructions", label: t("source.mcp_instructions") },
          { key: "skills", label: t("source.skills") },
          { key: "hooks", label: t("source.hooks") },
          { key: "tool_output", label: t("source.tool_output") },
        ])}
        format={format.count}
        tick={format.compact}
        testId="runtime-trend-context"
      />
      <TrendCard
        title={t("cacheTitle")}
        headline={t("cacheHeadline")}
        empty={t("cacheEmpty")}
        kind="lines"
        rows={trends.cache}
        series={series([
          { key: "sessions", label: t("sessions") },
          { key: "runs", label: t("runs") },
        ])}
        format={format.percent}
        share
        testId="runtime-trend-cache"
      />
      <TrendCard
        title={t("costTitle")}
        headline={t("costHeadline")}
        empty={t("costEmpty")}
        kind="lines"
        rows={trends.cost}
        series={series([
          { key: "session", label: t("perSession") },
          { key: "run", label: t("perRun") },
          { key: "pr", label: t("perPr") },
        ])}
        format={format.money}
        testId="runtime-trend-cost"
      />
      <TrendCard
        title={t("hooksTitle")}
        headline={t("hooksHeadline")}
        empty={t("hooksEmpty")}
        kind="lines"
        rows={trends.hooks}
        series={series([
          { key: "p95", label: t("p95") },
          { key: "p50", label: t("p50") },
        ])}
        format={format.ms}
        testId="runtime-trend-hooks"
      />
      <TrendCard
        title={t("peakTitle")}
        headline={t("peakHeadline")}
        empty={t("peakEmpty")}
        kind="lines"
        rows={trends.peaks}
        series={series([
          { key: "p95", label: t("p95") },
          { key: "p50", label: t("p50") },
        ])}
        format={format.count}
        tick={format.compact}
        testId="runtime-trend-peak"
      />
    </div>
  );
}

/** The figures of the counted nights of the range, read in parallel; a past night's never change once counted. */
function RuntimeBody({ project, nights }: { project: string; nights: string[] }) {
  const t = useTranslations("curator.runtime");
  const format = useInsightFormat();
  const reads = useQueries({ queries: nights.map((night) => figuresQuery(browserApi, project, night)) });
  if (nights.length === 0) {
    return <EmptyState icon={Layers} title={t("empty.title")} description={t("empty.description")} testId="runtime-empty" />;
  }
  if (reads.some((read) => read.isPending)) return <TrendSkeleton cards={4} />;
  const failed = reads.find((read) => read.isError)?.error;
  if (failed && reads.every((read) => read.isError)) return <ApiErrorState error={failed.info} onRetry={() => reads.forEach((read) => void read.refetch())} />;
  const found = reads
    .map((read, index) => (read.data ? runtimeNight(nights[index], read.data.figures) : null))
    .filter((night): night is RuntimeNight => night !== null);
  if (found.length === 0) {
    return <EmptyState icon={Layers} title={t("empty.title")} description={t("empty.olderFigures")} testId="runtime-empty" />;
  }
  const latest = found[0];
  const unread = reads.filter((read) => read.isError).length;
  return (
    <>
      <RuntimeStrip night={latest} />
      {unread > 0 ? (
        <p className="text-xs text-danger" role="status" data-testid="runtime-unread">
          {t("unread", { count: unread })}
        </p>
      ) : null}
      <Trends nights={found} charsPerToken={latest.charsPerToken ?? 4} />
      <h2 className="text-[15px] leading-[22px] font-semibold">{t("detailTitle", { night: format.day(latest.night) })}</h2>
      <RuntimeDetails project={project} night={latest} />
    </>
  );
}

/**
 * The runtime and the harness over time (`/p/{project}/curator/runtime`): from the figures of each counted night of
 * the range, the tokens each source of context put into a session, the cache's share of what the model read, the cost
 * per session, per run and per merged pull request, the slowest hook's times and the sessions' largest context; then
 * the latest night in detail. Each night counts the days of the charter's review before it.
 */
export function RuntimePage({ project, initialError }: { project: string; initialError: ApiErrorInfo | null }) {
  const t = useTranslations("curator.runtime");
  const [range, setRange] = useRange();
  const status = useCuratorStatus(project, initialError);
  const list = useHubQuery(nightsQuery(browserApi, project, range));
  return (
    <QueryView state={status} loading={<CuratorHeadSkeleton />}>
      {(found: CuratorStatus) => (
        <div className="flex flex-col gap-6" data-testid="runtime-page">
          <CuratorHeader project={project} status={found} />
          <CuratorTabs project={project} waiting={found.open_proposals} />
          <div className="flex flex-wrap items-center gap-x-3 gap-y-2" data-testid="runtime-range">
            <Segmented
              label={t("range.label")}
              value={String(range)}
              onChange={(value) => setRange(readRuntimeRange(value))}
              options={RUNTIME_RANGES.map((nights) => ({ value: String(nights), label: t("range.nights", { nights }), testId: `runtime-range-${nights}` }))}
            />
            <p className="text-xs text-fg-subtle">{t("range.window", { days: found.charter?.review?.days ?? 7 })}</p>
          </div>
          <QueryView state={list} loading={<TrendSkeleton cards={4} />}>
            {(nights) => <RuntimeBody project={project} nights={countedNights(nights.nights, range)} />}
          </QueryView>
        </div>
      )}
    </QueryView>
  );
}
