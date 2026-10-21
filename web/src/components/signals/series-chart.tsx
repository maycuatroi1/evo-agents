"use client";

import { useFormatter, useTranslations } from "next-intl";
import { CartesianGrid, Line, LineChart, type TooltipContentProps, XAxis, YAxis } from "recharts";

import { type ChartConfig, ChartContainer, ChartTooltip } from "@/components/ui/chart";
import { cn } from "@/lib/utils";

import { type Point, SERIES_HEIGHT } from "./model";

const FOCUS =
  "[&_.recharts-surface:focus-visible]:outline-2 [&_.recharts-surface:focus-visible]:outline-offset-2 [&_.recharts-surface:focus-visible]:outline-solid [&_.recharts-surface:focus-visible]:outline-ring";
const MARGIN = { top: 8, right: 12, bottom: 0, left: 0 };
const CONFIG = { value: { label: "value", color: "var(--chart-1)" } } satisfies ChartConfig;

/**
 * A key's values over time, drawn by Recharts through shadcn's chart (web/DESIGN.md, Charts) and loaded on demand by the
 * page (`next/dynamic`), so Recharts stays out of its first load: one 2 px line in `chart-1` through a dot at each
 * signal, hairline rules in `chart-grid`, the time axis by day. The chart is Recharts' keyboard surface (Tab, then the
 * arrow keys move the tooltip from signal to signal); the table under it holds the same values for screen readers.
 */
export default function SeriesChart({ points, unit, label }: { points: readonly Point[]; unit: string; label: string }) {
  const t = useTranslations("signals.series");
  const format = useFormatter();
  const value = (number: number) => `${format.number(number, { maximumFractionDigits: 4 })}${unit ? ` ${unit}` : ""}`;
  const tooltip = (props: TooltipContentProps) => {
    if (!props.active || !props.payload?.length) return null;
    const point = props.payload[0]?.payload as Point | undefined;
    if (!point) return null;
    return (
      <div role="status" aria-live="polite" aria-atomic="true" className="grid min-w-40 gap-1 rounded-md border bg-popover px-3 py-2 text-xs text-popover-foreground shadow-popover" data-testid="series-tooltip">
        <p className="font-medium text-foreground">{format.dateTime(new Date(point.at), { dateStyle: "medium", timeStyle: "short" })}</p>
        <p className="text-foreground tabular-nums">{value(point.value)}</p>
        {point.tenant ? <p className="text-muted-foreground">{t("tenantOf", { tenant: point.tenant })}</p> : null}
        {point.synthetic ? <p className="text-review">{t("synthetic")}</p> : null}
      </div>
    );
  };
  return (
    <ChartContainer
      config={CONFIG}
      className={cn("aspect-auto w-full", FOCUS)}
      style={{ height: SERIES_HEIGHT }}
      initialDimension={{ width: 640, height: SERIES_HEIGHT }}
      data-testid="series-chart"
    >
      <LineChart data={[...points]} margin={MARGIN} aria-label={label}>
        <CartesianGrid vertical={false} stroke="var(--chart-grid)" />
        <XAxis
          dataKey="at"
          type="number"
          scale="time"
          domain={["dataMin", "dataMax"]}
          tickLine={false}
          axisLine={false}
          tickMargin={8}
          minTickGap={28}
          tickFormatter={(at: number) => format.dateTime(new Date(at), { month: "short", day: "numeric" })}
        />
        <YAxis tickLine={false} axisLine={false} width={56} tickFormatter={(number: number) => format.number(number, { notation: "compact", maximumFractionDigits: 2 })} />
        <ChartTooltip isAnimationActive={false} cursor={{ stroke: "var(--border-strong)", strokeWidth: 1 }} content={tooltip} />
        <Line
          dataKey="value"
          type="linear"
          stroke="var(--color-value)"
          strokeWidth={2}
          strokeLinecap="round"
          strokeLinejoin="round"
          dot={{ r: 2.5, fill: "var(--color-value)", stroke: "var(--card)", strokeWidth: 1 }}
          activeDot={{ r: 4, fill: "var(--color-value)", stroke: "var(--card)", strokeWidth: 2 }}
          isAnimationActive={false}
        />
      </LineChart>
    </ChartContainer>
  );
}
