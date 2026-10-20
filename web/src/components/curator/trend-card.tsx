"use client";

import { ChartColumn, Table2 } from "lucide-react";
import dynamic from "next/dynamic";
import { useTranslations } from "next-intl";
import { useId, useState } from "react";

import { Segmented } from "@/components/data/segmented";
import { Skeleton } from "@/components/ui/skeleton";
import { cn } from "@/lib/utils";

import type { TrendChartProps } from "./trend-charts";
import { hasValues, stackTotal, TREND_HEIGHT, type TrendFormat, type TrendKind, type TrendRow, type TrendSeries } from "./trend-model";

/** The chart's place while Recharts loads: the same height, so the card does not move when it arrives. */
function ChartSkeleton() {
  return <Skeleton className="w-full rounded-sm" style={{ height: TREND_HEIGHT }} />;
}

// One chunk for every trend chart of the Curator's pages, fetched once a card has rows to draw.
const TrendChart = dynamic<TrendChartProps>(() => import("./trend-charts"), { ssr: false, loading: ChartSkeleton });

const CELL = "h-9 px-3 text-right whitespace-nowrap tabular-nums first:pl-4 last:pr-4";

function Legend({ series, kind, title }: { series: TrendSeries[]; kind: TrendKind; title: string }) {
  const t = useTranslations("curator.trend");
  if (series.length < 2) return null;
  return (
    <ul aria-label={t("legend", { chart: title })} className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
      {series.map((item) => (
        <li key={item.key} className="flex items-center gap-1.5">
          <span aria-hidden="true" className={cn("shrink-0", kind === "bars" ? "size-2 rounded-[2px]" : "h-0.5 w-3 rounded-full")} style={{ background: item.color }} />
          {item.label}
        </li>
      ))}
    </ul>
  );
}

/**
 * The figures of a trend as a table: a row per night or measure, the newest first. The chart's twin for screen readers
 * (`visible` false: in the document, out of sight) and the table a person switches to. Shown, it scrolls inside its own
 * focusable region, its head kept in view.
 */
function TrendTable({
  title,
  rows,
  series,
  kind,
  format,
  visible,
  testId,
}: {
  title: string;
  rows: TrendRow[];
  series: TrendSeries[];
  kind: TrendKind;
  format: TrendFormat;
  visible: boolean;
  testId: string;
}) {
  const t = useTranslations("curator.trend");
  const total = kind === "bars" && series.length > 1;
  const keys = series.map((item) => item.key);
  const value = (held: number | null | undefined) => (typeof held === "number" ? format(held) : <span className="text-fg-subtle">{t("none")}</span>);
  const table = (
    <table className="w-full caption-bottom text-[13px] leading-[18px]" data-testid={`${testId}-table`} data-visible={visible}>
      <caption className={visible ? "sr-only" : undefined}>{t("caption", { chart: title })}</caption>
      <thead className="sticky top-0 z-[1] bg-surface-sunken">
        <tr className="border-b">
          <th scope="col" className={cn(CELL, "text-left text-xs font-medium text-muted-foreground")}>
            {t("when")}
          </th>
          {series.map((item) => (
            <th key={item.key} scope="col" className={cn(CELL, "text-xs font-medium text-muted-foreground")}>
              {item.label}
            </th>
          ))}
          {total ? (
            <th scope="col" className={cn(CELL, "text-xs font-medium text-muted-foreground")}>
              {t("total")}
            </th>
          ) : null}
        </tr>
      </thead>
      <tbody>
        {[...rows].reverse().map((row) => (
          <tr key={row.at} className="border-b last:border-b-0" data-at={row.at} data-testid={`${testId}-row`}>
            <th scope="row" className={cn(CELL, "text-left font-normal text-foreground")}>
              <time dateTime={row.at}>{row.title}</time>
            </th>
            {series.map((item) => (
              <td key={item.key} className={cn(CELL, "text-muted-foreground")} data-col={item.key}>
                {value(row.values[item.key])}
              </td>
            ))}
            {total ? <td className={cn(CELL, "text-foreground")}>{value(stackTotal(row.values, keys))}</td> : null}
          </tr>
        ))}
      </tbody>
    </table>
  );
  if (!visible) return <div className="sr-only">{table}</div>;
  return (
    <div
      role="region"
      aria-label={t("tableRegion", { chart: title })}
      tabIndex={0}
      className="max-h-[22rem] overflow-auto rounded-sm border focus-visible:outline-offset-[-2px]"
      data-testid={`${testId}-table-region`}
    >
      {table}
    </div>
  );
}

/**
 * One trend in its card: the title and one line of what it comes to, Chart or Table (the kit's segmented control),
 * then the legend and the chart with its table kept for screen readers, or the table shown instead. A trend without
 * a single value says so in one line instead of drawing empty axes.
 */
export function TrendCard({
  title,
  headline,
  kind,
  rows,
  series,
  format,
  tick,
  share,
  empty,
  testId,
}: {
  title: string;
  headline: string;
  kind: TrendKind;
  rows: TrendRow[];
  series: TrendSeries[];
  format: TrendFormat;
  tick?: TrendFormat;
  share?: boolean;
  /** What the card says when no row holds a value. */
  empty: string;
  testId: string;
}) {
  const t = useTranslations("curator.trend");
  const id = useId();
  const [view, setView] = useState<"chart" | "table">("chart");
  const filled = hasValues(
    rows,
    series.map((item) => item.key),
  );
  return (
    <section aria-labelledby={id} className="flex min-w-0 flex-col gap-3 rounded-md border bg-card p-4 shadow-raised" data-testid={testId} data-view={view}>
      <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-2">
        <div className="flex min-w-0 flex-1 basis-56 flex-col gap-0.5">
          <h2 id={id} className="text-[15px] leading-[22px] font-semibold">
            {title}
          </h2>
          <p className="text-[13px] leading-[18px] text-pretty text-muted-foreground" data-testid={`${testId}-headline`}>
            {filled ? headline : empty}
          </p>
        </div>
        {filled ? (
          <Segmented
            label={t("view", { chart: title })}
            value={view}
            onChange={setView}
            options={[
              { value: "chart", label: t("chart"), icon: ChartColumn, testId: `${testId}-view-chart` },
              { value: "table", label: t("table"), icon: Table2, testId: `${testId}-view-table` },
            ]}
          />
        ) : null}
      </div>
      {!filled ? null : view === "chart" ? (
        <>
          <Legend series={series} kind={kind} title={title} />
          <div style={{ minHeight: TREND_HEIGHT }}>
            <TrendChart kind={kind} rows={rows} series={series} format={format} tick={tick} share={share} label={t("chartKeys", { chart: title })} testId={testId} />
          </div>
          <TrendTable title={title} rows={rows} series={series} kind={kind} format={format} visible={false} testId={testId} />
        </>
      ) : (
        <TrendTable title={title} rows={rows} series={series} kind={kind} format={format} visible testId={testId} />
      )}
    </section>
  );
}

/** The cards while their rows load: the head, the toggle and the chart's height. */
export function TrendSkeleton({ cards = 2 }: { cards?: number }) {
  return (
    <div className="grid min-w-0 gap-5 lg:grid-cols-2" aria-hidden="true">
      {Array.from({ length: cards }, (_, index) => (
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
