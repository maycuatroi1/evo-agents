"use client";

import { ChartColumn, Table2 } from "lucide-react";
import dynamic from "next/dynamic";
import Link from "next/link";
import { usePathname, useSearchParams } from "next/navigation";
import { useTranslations } from "next-intl";
import { type ComponentType, useId, useMemo, useState } from "react";

import { usePagedQuery } from "@/components/admin/use-paged-query";
import { Segmented } from "@/components/data/segmented";
import { PageHeader } from "@/components/shell/page-header";
import { projectHref } from "@/components/shell/nav";
import { QueryView } from "@/components/states/query-view";
import { EmptyState } from "@/components/states/states";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";
import { cn } from "@/lib/utils";

import { type InsightFormat, useInsightFormat } from "./format";
import { type InsightChart, InsightTable } from "./insights-tables";
import {
  CHART_HEIGHT,
  type DayRow,
  DURATION_SERIES,
  dayRows,
  type Figures,
  figuresOf,
  OUTCOME_SERIES,
  SERIES_COLORS,
  type SeriesKey,
  TOKEN_SERIES,
} from "./model";
import { INSIGHT_RANGES, type InsightRange, rangeSearch, readRange, type RunStats, runStatsQuery } from "./queries";

/** The chart's place while Recharts loads: the same height, so the card does not move when it arrives. */
function ChartSkeleton() {
  return <Skeleton className="w-full rounded-sm" style={{ height: CHART_HEIGHT }} />;
}

type ChartComponent = ComponentType<{ rows: DayRow[]; label: string }>;

// One chunk for the four charts, fetched once the page has data to draw: Recharts is in no page's first load.
const CHARTS: Record<InsightChart, ChartComponent> = {
  outcomes: dynamic(() => import("./insights-charts").then((module) => module.OutcomesChart), { ssr: false, loading: ChartSkeleton }),
  failure: dynamic(() => import("./insights-charts").then((module) => module.FailureChart), { ssr: false, loading: ChartSkeleton }),
  duration: dynamic(() => import("./insights-charts").then((module) => module.DurationChart), { ssr: false, loading: ChartSkeleton }),
  tokens: dynamic(() => import("./insights-charts").then((module) => module.TokensChart), { ssr: false, loading: ChartSkeleton }),
};

/** The series each chart's legend names, bottom of the stack first; a chart of one series has none. */
const LEGENDS: Record<InsightChart, { keys: readonly SeriesKey[]; mark: "bar" | "line" }> = {
  outcomes: { keys: OUTCOME_SERIES, mark: "bar" },
  failure: { keys: [], mark: "line" },
  duration: { keys: DURATION_SERIES, mark: "line" },
  tokens: { keys: TOKEN_SERIES, mark: "bar" },
};

/** The range in the URL (`?days=7`; none for 30), changed through the History API as the lists' filters are. */
function useRange(): [InsightRange, (next: InsightRange) => void] {
  const params = useSearchParams();
  const pathname = usePathname();
  const range = readRange(params.get("days"));
  const set = (next: InsightRange) => window.history.replaceState(null, "", `${pathname}${rangeSearch(next)}`);
  return [range, set];
}

function useSeriesLabel() {
  const t = useTranslations("insights");
  return (key: SeriesKey): string => {
    switch (key) {
      case "done":
      case "failed":
      case "stopped":
      case "lost":
      case "cancelled":
        return t(`outcomes.series.${key}`);
      case "rate":
        return t("failure.series.rate");
      case "p50":
      case "p90":
        return t(`duration.series.${key}`);
      default:
        return t(`tokens.series.${key}`);
    }
  };
}

/** What each card says of the whole range, in one line under its title. */
function useHeadlines(total: Figures, format: InsightFormat): Record<InsightChart, string> {
  const t = useTranslations("insights");
  return {
    outcomes: t("outcomes.headline", {
      ended: total.ended,
      done: format.count(total.done),
      failed: format.count(total.failed),
      lost: format.count(total.lost),
      cancelled: format.count(total.cancelled),
    }),
    failure:
      total.rate === null
        ? t("failure.headlineNone")
        : t("failure.headline", { rate: format.percent(total.rate), counted: total.counted }),
    duration:
      total.p50 === null || total.p90 === null
        ? t("duration.headlineNone")
        : t("duration.headline", { p50: format.duration(total.p50), p90: format.duration(total.p90) }),
    tokens: total.withUsage === 0 ? t("tokens.headlineNone") : t("tokens.headline", { count: total.tokens, runs: total.withUsage }),
  };
}

function Legend({ chart, title }: { chart: InsightChart; title: string }) {
  const t = useTranslations("insights");
  const label = useSeriesLabel();
  const { keys, mark } = LEGENDS[chart];
  if (keys.length < 2) return null;
  return (
    <ul aria-label={t("legend", { chart: title })} className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground" data-testid={`insights-${chart}-legend`}>
      {keys.map((key) => (
        <li key={key} className="flex items-center gap-1.5">
          <span
            aria-hidden="true"
            className={cn("shrink-0", mark === "bar" ? "size-2 rounded-[2px]" : "h-0.5 w-3 rounded-full")}
            style={{ background: SERIES_COLORS[key] }}
          />
          {label(key)}
        </li>
      ))}
    </ul>
  );
}

/**
 * One chart in its card: the title and what the range adds up to, Chart or Table (the kit's segmented control), then
 * the legend and the chart with its table kept for screen readers, or the table shown instead.
 */
function ChartCard({
  chart,
  title,
  headline,
  rows,
  total,
  format,
  span,
}: {
  chart: InsightChart;
  title: string;
  headline: string;
  rows: DayRow[];
  total: Figures;
  format: InsightFormat;
  span: { first: string; last: string };
}) {
  const t = useTranslations("insights");
  const id = useId();
  const [view, setView] = useState<"chart" | "table">("chart");
  const Chart = CHARTS[chart];
  return (
    <section aria-labelledby={id} className="flex min-w-0 flex-col gap-3 rounded-md border bg-card p-4 shadow-raised" data-testid={`insights-${chart}`} data-view={view}>
      <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-2">
        <div className="flex min-w-0 flex-1 basis-56 flex-col gap-0.5">
          <h2 id={id} className="text-[15px] leading-[22px] font-semibold">
            {title}
          </h2>
          <p className="text-[13px] leading-[18px] text-pretty text-muted-foreground" data-testid={`insights-${chart}-headline`}>
            {headline}
          </p>
        </div>
        <Segmented
          label={t("view.label", { chart: title })}
          value={view}
          onChange={setView}
          options={[
            { value: "chart", label: t("view.chart"), icon: ChartColumn, testId: `insights-${chart}-view-chart` },
            { value: "table", label: t("view.table"), icon: Table2, testId: `insights-${chart}-view-table` },
          ]}
        />
      </div>
      {view === "chart" ? (
        <>
          <Legend chart={chart} title={title} />
          <div style={{ minHeight: CHART_HEIGHT }}>
            <Chart rows={rows} label={t("chartKeys", { chart: title })} />
          </div>
          <InsightTable chart={chart} title={title} rows={rows} total={total} format={format} span={span} visible={false} />
        </>
      ) : (
        <InsightTable chart={chart} title={title} rows={rows} total={total} format={format} span={span} visible />
      )}
    </section>
  );
}

function Charts({ stats, project, range, onRange }: { stats: RunStats; project: string; range: InsightRange; onRange: (next: InsightRange) => void }) {
  const t = useTranslations("insights");
  const format = useInsightFormat();
  const rows = useMemo(() => dayRows(stats), [stats]);
  const total = useMemo(() => figuresOf(stats.total), [stats]);
  const headlines = useHeadlines(total, format);
  const span = { first: format.shortDay(stats.first_day), last: format.shortDay(stats.last_day) };
  if (total.ended === 0) {
    return (
      <EmptyState icon={ChartColumn} title={t("empty.title", { days: stats.days })} description={t("empty.description")}>
        <Button asChild variant="secondary">
          <Link href={projectHref(project, "runs")}>{t("empty.runs")}</Link>
        </Button>
        {range < 90 ? (
          <Button variant="ghost" onClick={() => onRange(90)} data-testid="insights-empty-longer">
            {t("empty.longer")}
          </Button>
        ) : null}
      </EmptyState>
    );
  }
  const cards: InsightChart[] = ["outcomes", "failure", "duration", "tokens"];
  return (
    <div className="grid min-w-0 gap-5 lg:grid-cols-2" data-testid="insights-charts">
      {cards.map((chart) => (
        <ChartCard
          key={chart}
          chart={chart}
          title={t(`${chart}.title`)}
          headline={headlines[chart]}
          rows={rows}
          total={total}
          format={format}
          span={span}
        />
      ))}
    </div>
  );
}

/** The cards while the first answer loads: the head, the toggle and the chart's height. */
function InsightsSkeleton() {
  return (
    <div className="grid gap-5 lg:grid-cols-2">
      {Array.from({ length: 4 }, (_, index) => (
        <div key={index} className="flex flex-col gap-3 rounded-md border bg-card p-4 shadow-raised">
          <div className="flex items-start justify-between gap-4">
            <div className="flex flex-col gap-1.5">
              <Skeleton className="h-4 w-32 rounded-xs" />
              <Skeleton className="h-3 w-56 max-w-full rounded-xs" />
            </div>
            <Skeleton className="h-7 w-32 rounded-sm" />
          </div>
          <ChartSkeleton />
        </div>
      ))}
    </div>
  );
}

/**
 * A project's Insights (`/p/{project}/insights`): the runs that ended on each UTC day of the last 7, 30 or 90 days,
 * charted four ways (by outcome, the failure rate, how long they ran, the tokens they used), each also a table. The
 * range lives in the URL; while the next range loads the charts keep the last one, faded.
 */
export function InsightsPage({ project, initialError }: { project: string; initialError: ApiErrorInfo | null }) {
  const t = useTranslations("insights");
  const format = useInsightFormat();
  const [range, setRange] = useRange();
  const { state, stale } = usePagedQuery(runStatsQuery(browserApi, project, range), initialError);
  const span =
    state.status === "success"
      ? t("range.span", { first: format.shortDay(state.data.first_day), last: format.shortDay(state.data.last_day) })
      : null;
  return (
    <>
      <div className="flex flex-col gap-4">
        <PageHeader title={t("title")} />
        <div className="flex flex-wrap items-center gap-x-3 gap-y-2" data-testid="insights-range">
          <Segmented
            label={t("range.label")}
            value={String(range)}
            onChange={(value) => setRange(readRange(value))}
            options={INSIGHT_RANGES.map((days) => ({ value: String(days), label: t("range.days", { days }), testId: `insights-range-${days}` }))}
          />
          {span ? (
            <p className="text-xs text-fg-subtle" data-testid="insights-span">
              {span}
            </p>
          ) : null}
        </div>
      </div>
      <div aria-busy={stale || undefined} className={cn("min-w-0 transition-opacity", stale && "opacity-60")}>
        <QueryView state={state} loading={<InsightsSkeleton />}>
          {(stats) => <Charts stats={stats} project={project} range={range} onRange={setRange} />}
        </QueryView>
      </div>
    </>
  );
}
