"use client";

import { Bug, ChartNoAxesColumn, GitMerge, Rewind, Undo2 } from "lucide-react";
import type { Route } from "next";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useTranslations } from "next-intl";
import { useCallback, useMemo } from "react";

import { DataCard } from "@/components/data/data-card";
import { CellMain, DataTable, dataTableColumns } from "@/components/data/data-table";
import { MetricStrip } from "@/components/data/metric-strip";
import { Segmented } from "@/components/data/segmented";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { EmptyState, ListSkeleton } from "@/components/states/states";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";
import { durationParts } from "@/lib/kg/format";
import { FIGURE_DAYS, type MergeFigure, mergeFiguresQuery, type MergeFigures } from "@/lib/merge-queue";

import { MergeQueueHeader } from "./chrome";
import { readDays } from "./model";

/** "45s", "8m 12s", "26h 5m", or "-" when nothing was measured. */
function useDuration() {
  const t = useTranslations("mergeQueue.duration");
  return useCallback(
    (seconds: number | null | undefined) => {
      if (seconds === null || seconds === undefined) return "-";
      const parts = durationParts(seconds * 1000);
      if (parts.unit === "underSecond") return t("under");
      if (parts.unit === "seconds") return t("seconds", { seconds: parts.seconds });
      if (parts.unit === "minutes") return t("minutes", { minutes: parts.minutes, seconds: parts.seconds });
      return t("hours", { hours: parts.hours, minutes: parts.minutes });
    },
    [t],
  );
}

function Numeric({ value, warn = false }: { value: number; warn?: boolean }) {
  return <span className={value === 0 ? "text-fg-subtle" : warn ? "font-medium text-danger" : "text-foreground"}>{value}</span>;
}

function FiguresTable({ figures }: { figures: MergeFigure[] }) {
  const t = useTranslations("mergeQueue.figures");
  const duration = useDuration();
  const columns = useMemo(() => {
    const helper = dataTableColumns<MergeFigure>();
    return helper.columns([
      helper.accessor((row) => `${row.repo} ${row.agent ?? ""} ${row.model ?? ""}`, {
        id: "who",
        header: () => t("who"),
        sortFn: "text",
        meta: { primary: true },
        cell: (info) => {
          const row = info.row.original;
          const who = [row.agent ?? t("person"), row.model].filter(Boolean).join(" / ");
          return (
            <CellMain sub={who} subTitle={who}>
              <span className="truncate font-mono text-[13px]">{row.repo}</span>
            </CellMain>
          );
        },
      }),
      helper.accessor("merged", { header: () => t("merged"), sortFn: "basic", meta: { numeric: true }, cell: (info) => <Numeric value={info.getValue()} /> }),
      helper.accessor("reverted", { header: () => t("reverted"), sortFn: "basic", meta: { numeric: true }, cell: (info) => <Numeric value={info.getValue()} warn /> }),
      helper.accessor("rolled_back", { header: () => t("rolledBack"), sortFn: "basic", meta: { numeric: true }, cell: (info) => <Numeric value={info.getValue()} warn /> }),
      helper.accessor("judge_misses", { header: () => t("judgeMisses"), sortFn: "basic", meta: { numeric: true }, cell: (info) => <Numeric value={info.getValue()} warn /> }),
      helper.accessor((row) => row.wait_seconds ?? -1, {
        id: "wait",
        header: () => t("wait"),
        sortFn: "basic",
        meta: { numeric: true },
        cell: (info) => (
          <span title={t("waitMax", { max: duration(info.row.original.wait_seconds_max) })}>{duration(info.row.original.wait_seconds)}</span>
        ),
      }),
      helper.accessor((row) => row.uat_seconds ?? -1, { id: "uat", header: () => t("toUat"), sortFn: "basic", meta: { numeric: true }, cell: (info) => duration(info.row.original.uat_seconds) }),
      helper.accessor((row) => row.prod_seconds ?? -1, { id: "prod", header: () => t("toProd"), sortFn: "basic", meta: { numeric: true }, cell: (info) => duration(info.row.original.prod_seconds) }),
    ]);
  }, [t, duration]);
  return (
    <DataTable
      data={figures}
      columns={columns}
      caption={t("caption")}
      getRowId={(row) => `${row.repo}\u0000${row.agent ?? ""}\u0000${row.model ?? ""}`}
      initialSorting={[{ id: "merged", desc: true }]}
      columnClassNames={{ uat: "hidden lg:table-cell", prod: "hidden lg:table-cell", wait: "hidden md:table-cell" }}
      testId="merge-figures-table"
    />
  );
}

function FiguresBody({ figures }: { figures: MergeFigures }) {
  const t = useTranslations("mergeQueue.figures");
  const sum = (key: "merged" | "reverted" | "rolled_back" | "judge_misses") => figures.figures.reduce((total, row) => total + row[key], 0);
  if (figures.figures.length === 0) {
    return <EmptyState icon={ChartNoAxesColumn} title={t("empty.title", { days: figures.days })} description={t("empty.description")} />;
  }
  return (
    <>
      <MetricStrip
        label={t("totals", { days: figures.days })}
        testId="merge-figures-summary"
        metrics={[
          { id: "merged", label: t("merged"), icon: GitMerge, value: sum("merged"), meta: t("mergedMeta", { days: figures.days }) },
          { id: "reverted", label: t("reverted"), icon: Undo2, value: sum("reverted"), meta: t("revertedMeta") },
          { id: "rolled-back", label: t("rolledBack"), icon: Rewind, value: sum("rolled_back"), meta: t("rolledBackMeta") },
          { id: "judge-misses", label: t("judgeMisses"), icon: Bug, value: sum("judge_misses"), tone: "attention", meta: t("judgeMissesMeta") },
        ]}
      />
      <DataCard testId="merge-figures">
        <FiguresTable figures={figures.figures} />
      </DataCard>
    </>
  );
}

/**
 * The merge queue's figures over the last 7, 30 or 90 days (`?days=`), for any member with a grant: for each repo,
 * agent and model, the merges, those reverted or rolled back, the Judge's passes found wrong, and how long a merge
 * request waited in the queue and took to pass its UAT and PROD checks.
 */
export function MergeFiguresPage({ project, initialError }: { project: string; initialError: ApiErrorInfo | null }) {
  const t = useTranslations("mergeQueue.figures");
  const params = useSearchParams();
  const router = useRouter();
  const pathname = usePathname();
  const days = readDays(params.get("days"));
  const state = useHubQuery(mergeFiguresQuery(browserApi, project, days), initialError);
  return (
    <div className="flex flex-col gap-6" data-testid="merge-figures-page" data-days={days}>
      <MergeQueueHeader
        project={project}
        current="figures"
        actions={
          <Segmented
            label={t("range")}
            value={String(days)}
            options={FIGURE_DAYS.map((value) => ({ value: String(value), label: t("days", { days: value }), testId: `merge-figures-days-${value}` }))}
            onChange={(value) => router.replace(`${pathname}?days=${value}` as Route, { scroll: false })}
          />
        }
      />
      <QueryView state={state} loading={<ListSkeleton rows={4} metrics={4} />}>
        {(found) => <FiguresBody figures={found} />}
      </QueryView>
    </div>
  );
}

