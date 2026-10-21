"use client";

import { useTranslations } from "next-intl";
import { Bar, BarChart, type BarShapeProps, CartesianGrid, Line, LineChart, type TooltipContentProps, XAxis, YAxis } from "recharts";

import { type ChartConfig, ChartContainer, ChartTooltip } from "@/components/ui/chart";
import { cn } from "@/lib/utils";

import {
  chartData,
  segmentPath,
  stackEnds,
  stackTotal,
  TREND_HEIGHT,
  type TrendFormat,
  type TrendKind,
  type TrendRow,
  type TrendSeries,
} from "./trend-model";

/**
 * The Curator's trend charts, drawn by Recharts through shadcn's chart (web/DESIGN.md, Charts) and loaded on demand
 * by the cards (`next/dynamic`), so Recharts stays out of every page's first load. Marks follow the Insights charts:
 * columns at most 24 px wide, a 4 px rounded end at the top of a stack and square at the baseline, 2 px of the card
 * between stacked segments, 2 px lines with a gap where a row has no value (a lone value gets an 8 px dot ringed in
 * `card`), hairline grid rules in `chart-grid`, no axis line. Each chart is Recharts' keyboard surface: Tab reaches it,
 * the left and right arrow keys move the tooltip from row to row, and the tooltip says every series of that row. The
 * figures are also in the card's table, for screen readers and for anyone who prefers it.
 */

const FOCUS =
  "[&_.recharts-surface:focus-visible]:outline-2 [&_.recharts-surface:focus-visible]:outline-offset-2 [&_.recharts-surface:focus-visible]:outline-solid [&_.recharts-surface:focus-visible]:outline-ring";
const MARGIN = { top: 8, right: 8, bottom: 0, left: 0 };
const GAP = 2;
const LINE_CURSOR = { stroke: "var(--border-strong)", strokeWidth: 1 };

type Datum = Record<string, string | number | null>;

export type TrendChartProps = {
  kind: TrendKind;
  rows: TrendRow[];
  series: TrendSeries[];
  format: TrendFormat;
  /** The axis's own, shorter format ("12k"); `format` when not given. */
  tick?: TrendFormat;
  /** A share from 0 to 1: the axis then runs from 0 to 100 %. */
  share?: boolean;
  label: string;
  testId: string;
};

/** A row of the tooltip: a series with its mark, or the stack's total (no colour, no mark). */
type TooltipLine = { key: string; label: string; value: string; color: string | null; mark: TrendKind };

function TooltipCard({ title, lines, testId }: { title: string; lines: TooltipLine[]; testId: string }) {
  return (
    <div
      role="status"
      aria-live="polite"
      aria-atomic="true"
      className="grid min-w-44 gap-1.5 rounded-md border bg-popover px-3 py-2 text-xs text-popover-foreground shadow-popover"
      data-testid={`${testId}-tooltip`}
    >
      <p className="font-medium text-foreground">{title}</p>
      <dl className="grid grid-cols-[auto_minmax(0,1fr)_auto] items-center gap-x-2 gap-y-1">
        {lines.map((line) => (
          <div key={line.key} className="contents">
            <dt className="contents">
              <span
                aria-hidden="true"
                className={cn(line.color === null ? "w-3" : line.mark === "bars" ? "size-2 rounded-[2px]" : "h-0.5 w-3 rounded-full")}
                style={line.color ? { background: line.color } : undefined}
              />
              <span className={cn("truncate", line.color === null ? "text-foreground" : "text-muted-foreground")}>{line.label}</span>
            </dt>
            <dd className="text-right font-medium text-foreground tabular-nums">{line.value}</dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

/** A column's segment of one series: the stack's top rounded, each one above the lowest 2 px off the one below. */
function stackShape(key: string, keys: readonly string[], testId: string) {
  return function StackSegment(props: BarShapeProps) {
    const { x, y, width, height, fill } = props;
    const datum = props.payload as Datum;
    const value = datum[key];
    if (typeof value !== "number" || !(value > 0) || !(height > 0) || !(width > 0)) return <g />;
    const values = Object.fromEntries(keys.map((name) => [name, typeof datum[name] === "number" ? (datum[name] as number) : null]));
    const { bottom, top } = stackEnds(values, keys);
    const lift = key === bottom ? 0 : Math.max(0, Math.min(GAP, height - 1));
    return <path d={segmentPath(x, y, width, height - lift, key === top)} fill={fill} data-testid={`${testId}-bar`} data-series={key} data-at={String(datum.at)} />;
  };
}

/** A point where the line's neighbours have no value, so a lone value still shows. */
function lonePoint(data: Datum[], key: string, color: string) {
  return function LonePoint(props: { cx?: number; cy?: number; index?: number }) {
    const index = props.index ?? -1;
    const held = (at: number) => typeof data[at]?.[key] === "number";
    if (!held(index) || held(index - 1) || held(index + 1) || props.cx === undefined || props.cy === undefined) return <g key={`${key}-${index}`} />;
    return <circle key={`${key}-${index}`} cx={props.cx} cy={props.cy} r={4} fill={color} stroke="var(--card)" strokeWidth={2} />;
  };
}

/** A trend as stacked columns or as lines, one row per night or measure. */
export default function TrendChart({ kind, rows, series, format, tick, share = false, label, testId }: TrendChartProps) {
  const t = useTranslations("curator.trend");
  const keys = series.map((item) => item.key);
  // A stack takes a missing value as 0 (Recharts cannot stack a null); the tooltip still says None for it.
  const data = chartData(rows).map((datum) => (kind === "bars" ? { ...datum, ...Object.fromEntries(keys.map((key) => [key, datum[key] ?? 0])) } : datum));
  const byAt = new Map(rows.map((row) => [row.at, row.values]));
  const config: ChartConfig = Object.fromEntries(series.map((item) => [item.key, { label: item.label, color: item.color }]));
  const axisFormat = tick ?? format;
  const tooltip = (props: TooltipContentProps) => {
    if (!props.active || !props.payload?.length) return null;
    const datum = props.payload[0]?.payload as Datum | undefined;
    const held = datum ? byAt.get(String(datum.at)) : undefined;
    if (!datum || !held) return null;
    const values = Object.fromEntries(keys.map((key) => [key, typeof held[key] === "number" ? held[key] : null]));
    const ordered = kind === "bars" ? [...series].reverse() : series; // the top of the stack first, as the column reads
    const lines: TooltipLine[] = ordered.map((item) => ({
      key: item.key,
      label: item.label,
      value: values[item.key] === null ? t("none") : format(values[item.key] as number),
      color: item.color,
      mark: kind,
    }));
    const total = kind === "bars" && series.length > 1 ? stackTotal(values, keys) : null;
    if (total !== null) lines.push({ key: "total", label: t("total"), value: format(total), color: null, mark: kind });
    return <TooltipCard title={String(datum.title)} lines={lines} testId={testId} />;
  };
  const grid = <CartesianGrid vertical={false} stroke="var(--chart-grid)" />;
  const x = <XAxis dataKey="tick" tickLine={false} axisLine={false} tickMargin={8} minTickGap={20} />;
  const y = (
    <YAxis
      tickLine={false}
      axisLine={false}
      width={48}
      {...(share ? { domain: [0, 1], ticks: [0, 0.25, 0.5, 0.75, 1] } : { allowDecimals: false })}
      tickFormatter={(value: number) => axisFormat(value)}
    />
  );
  return (
    <ChartContainer
      config={config}
      className={cn("aspect-auto w-full", FOCUS)}
      style={{ height: TREND_HEIGHT }}
      initialDimension={{ width: 480, height: TREND_HEIGHT }}
      data-testid={`${testId}-chart`}
    >
      {kind === "bars" ? (
        <BarChart data={data} margin={MARGIN} barCategoryGap="18%" aria-label={label}>
          {grid}
          {x}
          {y}
          <ChartTooltip isAnimationActive={false} content={tooltip} />
          {series.map((item) => (
            <Bar
              key={item.key}
              dataKey={item.key}
              stackId="row"
              fill={`var(--color-${item.key})`}
              maxBarSize={24}
              isAnimationActive={false}
              shape={stackShape(item.key, keys, testId)}
            />
          ))}
        </BarChart>
      ) : (
        <LineChart data={data} margin={MARGIN} aria-label={label}>
          {grid}
          {x}
          {y}
          <ChartTooltip isAnimationActive={false} filterNull={false} cursor={LINE_CURSOR} content={tooltip} />
          {series.map((item) => (
            <Line
              key={item.key}
              dataKey={item.key}
              type="linear"
              stroke={`var(--color-${item.key})`}
              strokeWidth={2}
              strokeLinecap="round"
              strokeLinejoin="round"
              connectNulls={false}
              dot={lonePoint(data, item.key, item.color)}
              activeDot={{ r: 4, fill: item.color, stroke: "var(--card)", strokeWidth: 2 }}
              isAnimationActive={false}
            />
          ))}
        </LineChart>
      )}
    </ChartContainer>
  );
}
