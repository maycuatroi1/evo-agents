"use client";

import { useTranslations } from "next-intl";
import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

import type { InsightFormat } from "./format";
import type { DayRow, Figures } from "./model";

/** The four charts of the page; each has a table with the same figures. */
export type InsightChart = "outcomes" | "failure" | "duration" | "tokens";

type Column = {
  key: string;
  header: string;
  cell: (figures: Figures) => ReactNode;
};

/** A value the API has none for: a failure rate or a duration of a day without the runs it needs. */
function Missing() {
  const t = useTranslations("insights");
  return <span className="text-fg-subtle">{t("none")}</span>;
}

function useColumns(chart: InsightChart, format: InsightFormat): Column[] {
  const t = useTranslations("insights");
  const count = (value: number) => format.count(value);
  switch (chart) {
    case "outcomes":
      return [
        { key: "done", header: t("outcomes.series.done"), cell: (row) => count(row.done) },
        { key: "failed", header: t("outcomes.series.failed"), cell: (row) => count(row.failed) },
        { key: "lost", header: t("outcomes.series.lost"), cell: (row) => count(row.lost) },
        { key: "cancelled", header: t("outcomes.series.cancelled"), cell: (row) => count(row.cancelled) },
        { key: "ended", header: t("outcomes.series.total"), cell: (row) => count(row.ended) },
      ];
    case "failure":
      return [
        { key: "rate", header: t("failure.series.rate"), cell: (row) => (row.rate === null ? <Missing /> : format.percent(row.rate)) },
        { key: "failing", header: t("failure.series.failing"), cell: (row) => count(row.failing) },
        { key: "counted", header: t("failure.series.counted"), cell: (row) => count(row.counted) },
      ];
    case "duration":
      return [
        { key: "p50", header: t("duration.series.p50"), cell: (row) => (row.p50 === null ? <Missing /> : format.duration(row.p50)) },
        { key: "p90", header: t("duration.series.p90"), cell: (row) => (row.p90 === null ? <Missing /> : format.duration(row.p90)) },
      ];
    case "tokens":
      return [
        { key: "cacheRead", header: t("tokens.series.cacheRead"), cell: (row) => count(row.cacheRead) },
        { key: "input", header: t("tokens.series.input"), cell: (row) => count(row.input) },
        { key: "output", header: t("tokens.series.output"), cell: (row) => count(row.output) },
        { key: "reasoning", header: t("tokens.series.reasoning"), cell: (row) => count(row.reasoning) },
        { key: "tokens", header: t("tokens.series.total"), cell: (row) => count(row.tokens) },
      ];
  }
}

const CELL = "h-9 px-3 text-right whitespace-nowrap tabular-nums first:pl-4 last:pr-4";

/**
 * The figures of one chart as a table: a row per UTC day, the newest first, and the whole range in the foot. The
 * chart's twin for screen readers (`visible` false: in the document, out of sight) and the table a person switches to
 * from the card. Shown, it scrolls inside its own focusable region, its head kept in view, so 90 days never stretch the
 * page nor push it sideways.
 */
export function InsightTable({
  chart,
  title,
  rows,
  total,
  format,
  span,
  visible,
}: {
  chart: InsightChart;
  title: string;
  rows: readonly DayRow[];
  total: Figures;
  format: InsightFormat;
  /** The range in words, "Sep 8 to Oct 7". */
  span: { first: string; last: string };
  visible: boolean;
}) {
  const t = useTranslations("insights");
  const columns = useColumns(chart, format);
  const newestFirst = [...rows].reverse();
  const table = (
    <table className="w-full caption-bottom text-[13px] leading-[18px]" data-testid={`insights-${chart}-table`} data-visible={visible}>
      <caption className={visible ? "sr-only" : undefined}>{t("caption", { chart: title, ...span })}</caption>
      <thead className="sticky top-0 z-[1] bg-surface-sunken">
        <tr className="border-b">
          <th scope="col" className={cn(CELL, "text-left text-xs font-medium text-muted-foreground")}>
            {t("day")}
          </th>
          {columns.map((column) => (
            <th key={column.key} scope="col" className={cn(CELL, "text-xs font-medium text-muted-foreground")}>
              {column.header}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {newestFirst.map((row) => (
          <tr key={row.day} className="border-b last:border-b-0" data-day={row.day} data-testid={`insights-${chart}-row`}>
            <th scope="row" className={cn(CELL, "text-left font-normal text-foreground")}>
              <time dateTime={row.day}>{format.day(row.day)}</time>
            </th>
            {columns.map((column) => (
              <td key={column.key} className={cn(CELL, "text-muted-foreground")} data-col={column.key}>
                {column.cell(row)}
              </td>
            ))}
          </tr>
        ))}
      </tbody>
      <tfoot className="border-t bg-surface-sunken">
        <tr data-testid={`insights-${chart}-total`}>
          <th scope="row" className={cn(CELL, "text-left font-medium text-foreground")}>
            {t("allDays", { days: rows.length })}
          </th>
          {columns.map((column) => (
            <td key={column.key} className={cn(CELL, "font-medium text-foreground")} data-col={column.key}>
              {column.cell(total)}
            </td>
          ))}
        </tr>
      </tfoot>
    </table>
  );
  if (!visible) return <div className="sr-only">{table}</div>;
  return (
    <div
      role="region"
      aria-label={t("tableRegion", { chart: title })}
      tabIndex={0}
      className="max-h-[22rem] overflow-auto rounded-sm border focus-visible:outline-offset-[-2px]"
      data-testid={`insights-${chart}-table-region`}
    >
      {table}
    </div>
  );
}
