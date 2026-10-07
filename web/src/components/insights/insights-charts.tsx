"use client";

import { useTranslations } from "next-intl";
import type { ReactNode } from "react";
import { Bar, BarChart, type BarShapeProps, CartesianGrid, Line, LineChart, type TooltipContentProps, XAxis, YAxis } from "recharts";

import { type ChartConfig, ChartContainer, ChartTooltip } from "@/components/ui/chart";
import { cn } from "@/lib/utils";

import { type InsightFormat, useInsightFormat } from "./format";
import {
  CHART_HEIGHT,
  type DayRow,
  DURATION_SERIES,
  durationTicks,
  longestDuration,
  OUTCOME_SERIES,
  SERIES_COLORS,
  type SeriesKey,
  stackEnds,
  TOKEN_SERIES,
} from "./model";

/**
 * The Insights page's four charts, drawn by Recharts through shadcn's chart (web/DESIGN.md, Charts) and loaded on
 * demand by the page (`next/dynamic`), so Recharts stays out of every page's first load. Marks follow the kit and the
 * dataviz rules: columns at most 24 px wide, a 4 px rounded end at the top of a stack and square at the baseline, 2 px
 * of the card between stacked segments, 2 px lines, hairline grid rules in `chart-grid`, no axis line.
 *
 * Each chart is Recharts' keyboard surface (its accessibility layer): Tab reaches it, the left and right arrow keys
 * move the tooltip from day to day, and the tooltip says every series of that day. The figures are also in the card's
 * table, for screen readers and for anyone who prefers it.
 */

/** The chart's own focus outline: the kit's 2 px ring, which shadcn's container takes off the Recharts surface. */
const FOCUS = "[&_.recharts-surface:focus-visible]:outline-2 [&_.recharts-surface:focus-visible]:outline-offset-2 [&_.recharts-surface:focus-visible]:outline-solid [&_.recharts-surface:focus-visible]:outline-ring";

const MARGIN = { top: 8, right: 8, bottom: 0, left: 0 };
const GAP = 2;
const RADIUS = 4;

type ChartProps = { rows: DayRow[]; label: string };

function configOf(keys: readonly SeriesKey[], label: (key: SeriesKey) => string): ChartConfig {
  return Object.fromEntries(keys.map((key) => [key, { label: label(key), color: SERIES_COLORS[key] }]));
}

/** A column's segment: square, or with the stack's rounded data end on top. */
function segmentPath(x: number, y: number, width: number, height: number, round: boolean): string {
  const r = round ? Math.min(RADIUS, width / 2, height) : 0;
  if (r <= 0) return `M${x},${y}h${width}v${height}h${-width}Z`;
  return `M${x},${y + height}V${y + r}A${r},${r} 0 0 1 ${x + r},${y}H${x + width - r}A${r},${r} 0 0 1 ${x + width},${y + r}V${y + height}Z`;
}

/**
 * The shape of one series of a stacked column: the top segment of the day rounded, each one above the lowest standing
 * 2 px off the one below. The path names its day, series and value, which is what the column draws.
 */
function stackShape<K extends SeriesKey>(series: K, keys: readonly K[], chart: string) {
  return function StackSegment(props: BarShapeProps) {
    const { x, y, width, height, fill } = props;
    const row = props.payload as DayRow & Record<K, number>;
    const value = row[series];
    if (!(value > 0) || !(height > 0) || !(width > 0)) return <g />;
    const { bottom, top } = stackEnds(row, keys);
    const lift = series === bottom ? 0 : Math.max(0, Math.min(GAP, height - 1));
    return (
      <path
        d={segmentPath(x, y, width, height - lift, series === top)}
        fill={fill}
        data-testid={`insights-${chart}-bar`}
        data-series={series}
        data-day={row.day}
        data-value={value}
      />
    );
  };
}

/** A row of the tooltip: a series with its mark, or a figure of the day without one (the total). */
type TooltipLine = { id: string; series: SeriesKey | null; label: string; value: string; mark?: "bar" | "line" };

/**
 * The tooltip of every chart: the day, then each series of it with its value, keyed by a short mark in the series'
 * colour; the value is the strong part, in tabular figures. A status region, so the day reached with the arrow keys is
 * read out.
 */
function TooltipCard({ title, lines }: { title: string; lines: TooltipLine[] }) {
  return (
    <div
      role="status"
      aria-live="polite"
      aria-atomic="true"
      className="grid min-w-44 gap-1.5 rounded-md border bg-popover px-3 py-2 text-xs text-popover-foreground shadow-popover"
      data-testid="insights-tooltip"
    >
      <p className="font-medium text-foreground">{title}</p>
      <dl className="grid grid-cols-[auto_minmax(0,1fr)_auto] items-center gap-x-2 gap-y-1">
        {lines.map((line) => (
          <div key={line.id} className="contents" data-line={line.id}>
            <dt className="contents">
              <span
                aria-hidden="true"
                className={cn(line.mark === "bar" ? "size-2 rounded-[2px]" : line.mark === "line" ? "h-0.5 w-3 rounded-full" : "w-3")}
                style={line.series ? { background: SERIES_COLORS[line.series] } : undefined}
              />
              <span className={cn("truncate", line.id === "total" ? "text-foreground" : "text-muted-foreground")}>{line.label}</span>
            </dt>
            <dd className="text-right font-medium text-foreground tabular-nums">{line.value}</dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

function rowOf(props: TooltipContentProps): DayRow | null {
  if (!props.active || !props.payload?.length) return null;
  return (props.payload[0]?.payload as DayRow | undefined) ?? null;
}

/** What every chart shares: the horizontal grid rules and the day axis, ticks thinned to fit. */
function useAxes(format: InsightFormat) {
  return {
    grid: <CartesianGrid vertical={false} stroke="var(--chart-grid)" />,
    x: (
      <XAxis
        dataKey="day"
        tickFormatter={(day: string) => format.shortDay(day)}
        tickLine={false}
        axisLine={false}
        tickMargin={8}
        minTickGap={20}
      />
    ),
  };
}

function ChartFrame({ config, children, testId }: { config: ChartConfig; children: ReactNode; testId: string }) {
  return (
    <ChartContainer
      config={config}
      className={cn("aspect-auto w-full", FOCUS)}
      style={{ height: CHART_HEIGHT }}
      initialDimension={{ width: 480, height: CHART_HEIGHT }}
      data-testid={testId}
    >
      {children}
    </ChartContainer>
  );
}

/** Runs that ended each day, stacked done, failed, and lost or cancelled. */
export function OutcomesChart({ rows, label }: ChartProps) {
  const t = useTranslations("insights.outcomes.series");
  const format = useInsightFormat();
  const axes = useAxes(format);
  const config = configOf(OUTCOME_SERIES, (key) => t(key as (typeof OUTCOME_SERIES)[number]));
  const tooltip = (props: TooltipContentProps) => {
    const row = rowOf(props);
    if (!row) return null;
    const line = (key: "done" | "failed" | "lost" | "cancelled"): TooltipLine => ({
      id: key,
      series: key,
      label: t(key),
      value: format.count(row[key]),
      mark: "bar",
    });
    return (
      <TooltipCard
        title={format.day(row.day)}
        lines={[line("done"), line("failed"), line("lost"), line("cancelled"), { id: "total", series: null, label: t("total"), value: format.count(row.ended) }]}
      />
    );
  };
  return (
    <ChartFrame config={config} testId="insights-outcomes-chart">
      <BarChart data={rows} margin={MARGIN} barCategoryGap="18%" aria-label={label}>
        {axes.grid}
        {axes.x}
        <YAxis tickLine={false} axisLine={false} width={40} allowDecimals={false} tickFormatter={(value: number) => format.count(value)} />
        <ChartTooltip isAnimationActive={false} content={tooltip} />
        {OUTCOME_SERIES.map((key) => (
          <Bar
            key={key}
            dataKey={key}
            stackId="day"
            fill={`var(--color-${key})`}
            maxBarSize={24}
            isAnimationActive={false}
            shape={stackShape(key, OUTCOME_SERIES, "outcomes")}
          />
        ))}
      </BarChart>
    </ChartFrame>
  );
}

/** A point of a line where its neighbours have no value, so a lone day still shows: 8 px, ringed in the card. */
function lonePoint(rows: DayRow[], key: "rate" | "p50" | "p90") {
  return function LonePoint(props: { cx?: number; cy?: number; index?: number }) {
    const index = props.index ?? -1;
    const alone = rows[index]?.[key] != null && rows[index - 1]?.[key] == null && rows[index + 1]?.[key] == null;
    if (!alone || props.cx === undefined || props.cy === undefined) return <g key={`${key}-${index}`} />;
    return <circle key={`${key}-${index}`} cx={props.cx} cy={props.cy} r={4} fill={SERIES_COLORS[key]} stroke="var(--card)" strokeWidth={2} />;
  };
}

const ACTIVE_DOT = (key: SeriesKey) => ({ r: 4, fill: SERIES_COLORS[key], stroke: "var(--card)", strokeWidth: 2 });
const LINE_CURSOR = { stroke: "var(--border-strong)", strokeWidth: 1 };
// The line charts keep a day without a value in the tooltip (`filterNull` off), which then says None for it, so the
// arrow keys never land on a day that shows nothing.

/** Failed or lost of the runs that ended done, failed or lost, each day; a day without such a run is a gap. */
export function FailureChart({ rows, label }: ChartProps) {
  const t = useTranslations("insights.failure.series");
  const tInsights = useTranslations("insights");
  const format = useInsightFormat();
  const axes = useAxes(format);
  const config = configOf(["rate"], () => t("rate"));
  const tooltip = (props: TooltipContentProps) => {
    const row = rowOf(props);
    if (!row) return null;
    return (
      <TooltipCard
        title={format.day(row.day)}
        lines={[
          { id: "rate", series: "rate", label: t("rate"), value: row.rate === null ? tInsights("none") : format.percent(row.rate), mark: "line" },
          { id: "failing", series: null, label: t("failing"), value: format.count(row.failing) },
          { id: "counted", series: null, label: t("counted"), value: format.count(row.counted) },
        ]}
      />
    );
  };
  return (
    <ChartFrame config={config} testId="insights-failure-chart">
      <LineChart data={rows} margin={MARGIN} aria-label={label}>
        {axes.grid}
        {axes.x}
        <YAxis
          tickLine={false}
          axisLine={false}
          width={40}
          domain={[0, 1]}
          ticks={[0, 0.25, 0.5, 0.75, 1]}
          tickFormatter={(value: number) => format.percent(value)}
        />
        <ChartTooltip isAnimationActive={false} filterNull={false} cursor={LINE_CURSOR} content={tooltip} />
        <Line
          dataKey="rate"
          type="linear"
          stroke="var(--color-rate)"
          strokeWidth={2}
          strokeLinecap="round"
          strokeLinejoin="round"
          connectNulls={false}
          dot={lonePoint(rows, "rate")}
          activeDot={ACTIVE_DOT("rate")}
          isAnimationActive={false}
        />
      </LineChart>
    </ChartFrame>
  );
}

/** The median and 90th percentile of how long the runs that ended each day ran. */
export function DurationChart({ rows, label }: ChartProps) {
  const t = useTranslations("insights.duration.series");
  const tInsights = useTranslations("insights");
  const format = useInsightFormat();
  const axes = useAxes(format);
  const config = configOf(DURATION_SERIES, (key) => t(key as (typeof DURATION_SERIES)[number]));
  const ticks = durationTicks(longestDuration(rows));
  const tooltip = (props: TooltipContentProps) => {
    const row = rowOf(props);
    if (!row) return null;
    const line = (key: "p50" | "p90"): TooltipLine => {
      const seconds = row[key];
      return { id: key, series: key, label: t(key), value: seconds === null ? tInsights("none") : format.duration(seconds), mark: "line" };
    };
    return <TooltipCard title={format.day(row.day)} lines={[line("p50"), line("p90")]} />;
  };
  return (
    <ChartFrame config={config} testId="insights-duration-chart">
      <LineChart data={rows} margin={MARGIN} aria-label={label}>
        {axes.grid}
        {axes.x}
        <YAxis
          tickLine={false}
          axisLine={false}
          width={48}
          domain={[0, ticks[ticks.length - 1]]}
          ticks={ticks}
          tickFormatter={(value: number) => format.durationTick(value)}
        />
        <ChartTooltip isAnimationActive={false} filterNull={false} cursor={LINE_CURSOR} content={tooltip} />
        {DURATION_SERIES.map((key) => (
          <Line
            key={key}
            dataKey={key}
            type="linear"
            stroke={`var(--color-${key})`}
            strokeWidth={2}
            strokeLinecap="round"
            strokeLinejoin="round"
            connectNulls={false}
            dot={lonePoint(rows, key)}
            activeDot={ACTIVE_DOT(key)}
            isAnimationActive={false}
          />
        ))}
      </LineChart>
    </ChartFrame>
  );
}

/** The tokens the runs that ended each day used, stacked by type as the run page's UsageMeter shows them. */
export function TokensChart({ rows, label }: ChartProps) {
  const t = useTranslations("insights.tokens.series");
  const format = useInsightFormat();
  const axes = useAxes(format);
  const config = configOf(TOKEN_SERIES, (key) => t(key as (typeof TOKEN_SERIES)[number]));
  const tooltip = (props: TooltipContentProps) => {
    const row = rowOf(props);
    if (!row) return null;
    // Top of the stack first, as the column reads from the top.
    const lines: TooltipLine[] = [...TOKEN_SERIES]
      .reverse()
      .map((key) => ({ id: key, series: key, label: t(key), value: format.count(row[key]), mark: "bar" }));
    return <TooltipCard title={format.day(row.day)} lines={[...lines, { id: "total", series: null, label: t("total"), value: format.count(row.tokens) }]} />;
  };
  return (
    <ChartFrame config={config} testId="insights-tokens-chart">
      <BarChart data={rows} margin={MARGIN} barCategoryGap="18%" aria-label={label}>
        {axes.grid}
        {axes.x}
        <YAxis tickLine={false} axisLine={false} width={44} allowDecimals={false} tickFormatter={(value: number) => format.compact(value)} />
        <ChartTooltip isAnimationActive={false} content={tooltip} />
        {TOKEN_SERIES.map((key) => (
          <Bar
            key={key}
            dataKey={key}
            stackId="day"
            fill={`var(--color-${key})`}
            maxBarSize={24}
            isAnimationActive={false}
            shape={stackShape(key, TOKEN_SERIES, "tokens")}
          />
        ))}
      </BarChart>
    </ChartFrame>
  );
}
