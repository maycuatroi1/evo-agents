"use client";

import Link from "next/link";
import { useTranslations } from "next-intl";
import { useMemo } from "react";

import { DataTable, dataTableColumns } from "@/components/data/data-table";
import { NAME_LINK } from "@/components/data/identifier";
import { clusterColour } from "@/lib/kg/map";
import { clusterHref } from "@/lib/kg/routes";
import type { ClusterSummary } from "@/lib/kg/types";
import { cn } from "@/lib/utils";

export function kindsText(kinds: Record<string, number>): string {
  return Object.entries(kinds)
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
    .map(([kind, count]) => `${kind} ${count}`)
    .join(", ");
}

export function Swatch({ index }: { index: number }) {
  return (
    <span
      aria-hidden="true"
      className="inline-block size-2.5 shrink-0 rounded-full"
      style={{ backgroundColor: clusterColour(index) }}
    />
  );
}

type Row = ClusterSummary & { index: number };

/** A cluster's parts as a table: each label opens the part's page; `focus` marks one of them. */
export function ClusterTable({
  project,
  clusters,
  focus,
  testId = "kg-cluster-table",
}: {
  project: string;
  clusters: ClusterSummary[];
  /** The index of the cluster lit up, or null. */
  focus?: number | null;
  testId?: string;
}) {
  const t = useTranslations("kg.map.table");
  const rows = useMemo<Row[]>(() => clusters.map((cluster, index) => ({ ...cluster, index })), [clusters]);
  const columns = useMemo(() => {
    const helper = dataTableColumns<Row>();
    return helper.columns([
      helper.accessor("label", {
        header: () => t("label"),
        sortFn: "text",
        cell: (info) => (
          <div className="flex min-w-0 items-center gap-2">
            <Swatch index={info.row.original.index} />
            <Link
              href={clusterHref(project, info.row.original.id)}
              className={cn(NAME_LINK, "break-words whitespace-normal", focus === info.row.original.index && "font-semibold")}
              aria-current={focus === info.row.original.index ? "true" : undefined}
              data-testid="kg-cluster-link"
              data-cluster-id={info.row.original.id}
            >
              {info.getValue() || info.row.original.id}
            </Link>
          </div>
        ),
      }),
      helper.accessor("size", {
        header: () => t("size"),
        sortFn: "basic",
        cell: (info) => <span className="tabular-nums">{info.getValue()}</span>,
      }),
      helper.accessor((row) => kindsText(row.kinds), {
        id: "kinds",
        header: () => t("kinds"),
        sortFn: "text",
        cell: (info) => <span className="text-xs text-muted-foreground">{info.getValue()}</span>,
      }),
      helper.accessor("subclusters", {
        header: () => t("subclusters"),
        sortFn: "basic",
        cell: (info) => <span className="tabular-nums">{info.getValue()}</span>,
      }),
    ]);
  }, [t, project, focus]);
  return (
    <DataTable
      data={rows}
      columns={columns}
      caption={t("caption", { count: rows.length })}
      getRowId={(row) => row.id}
      density="compact"
      columnClassNames={{ kinds: "hidden md:table-cell", subclusters: "hidden sm:table-cell" }}
      testId={testId}
    />
  );
}
