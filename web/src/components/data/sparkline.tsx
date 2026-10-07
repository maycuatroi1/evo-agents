"use client";

import { Area, AreaChart, Dot, ReferenceLine, YAxis } from "recharts";

import { type ChartConfig, ChartContainer } from "@/components/ui/chart";

/** The strip's sparkline: 22 px high, the width of its cell. */
export const SPARKLINE_HEIGHT = 22;

const CONFIG = { value: { label: "value", color: "var(--chart-1)" } } satisfies ChartConfig;

/**
 * A count over time in a metric cell, drawn by Recharts through shadcn's chart (web/DESIGN.md, Charts): the main measure
 * in `chart-1` over a `brand-soft` area, a `chart-grid` baseline at zero and a dot on the latest value. It is drawn for
 * the eye only; the cell names every value in words (`MetricStrip`), so the chart is hidden from assistive technology
 * and takes no focus. Loaded on demand by the strip (`next/dynamic`), so Recharts is not in a page's first load.
 */
export default function Sparkline({ values }: { values: readonly number[] }) {
  const data = values.map((value, index) => ({ index, value }));
  const last = data.length - 1;
  return (
    <ChartContainer
      config={CONFIG}
      className="aspect-auto h-full w-full"
      initialDimension={{ width: 140, height: SPARKLINE_HEIGHT }}
      data-testid="sparkline-chart"
    >
      <AreaChart data={data} margin={{ top: 3, right: 3, bottom: 1, left: 1 }} accessibilityLayer={false}>
        <YAxis hide domain={[0, (max: number) => Math.max(1, max)]} />
        <ReferenceLine y={0} stroke="var(--chart-grid)" strokeWidth={1} ifOverflow="extendDomain" />
        <Area
          dataKey="value"
          type="linear"
          stroke="var(--color-value)"
          strokeWidth={1.5}
          strokeLinejoin="round"
          strokeLinecap="round"
          fill="var(--brand-soft)"
          fillOpacity={1}
          isAnimationActive={false}
          activeDot={false}
          dot={(props: { cx?: number; cy?: number; index?: number }) =>
            props.index === last && props.cx !== undefined && props.cy !== undefined ? (
              <Dot key="last" cx={props.cx} cy={props.cy} r={2} fill="var(--color-value)" stroke="none" />
            ) : (
              <g key={props.index} />
            )
          }
        />
      </AreaChart>
    </ChartContainer>
  );
}
