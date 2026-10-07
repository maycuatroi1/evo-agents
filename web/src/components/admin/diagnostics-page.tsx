"use client";

import { useFormatter, useTranslations } from "next-intl";
import { useMemo } from "react";

import { DataCard } from "@/components/data/data-card";
import { CellMain, DataTable, dataTableColumns } from "@/components/data/data-table";
import { PageHeader } from "@/components/shell/page-header";
import { QueryView, useHubQuery } from "@/components/states/query-view";
import { TableSkeleton } from "@/components/states/states";
import { browserApi } from "@/lib/api/browser";
import type { ApiErrorInfo } from "@/lib/api/errors";

import { adminStatsQuery } from "./data";
import { type TableRow, tableRows } from "./overview-model";

function TableCounts({ stats }: { stats: Record<string, number> }) {
  const t = useTranslations("admin.diagnostics");
  const format = useFormatter();
  const { rows, total } = useMemo(() => tableRows(stats), [stats]);
  const columns = useMemo(() => {
    const helper = dataTableColumns<TableRow>();
    return helper.columns([
      helper.accessor("table", {
        header: () => t("columns.table"),
        sortFn: "text",
        meta: { primary: true },
        cell: (info) => (
          <CellMain>
            <span className="truncate font-mono text-[13px] font-normal" data-table={info.getValue()}>
              {info.getValue()}
            </span>
          </CellMain>
        ),
      }),
      helper.accessor((row) => t(`areas.${row.area}`), {
        id: "area",
        header: () => t("columns.area"),
        sortFn: "text",
        cell: (info) => info.getValue(),
      }),
      helper.accessor("rows", {
        header: () => t("columns.rows"),
        sortFn: "basic",
        meta: { numeric: true },
        cell: (info) => <span className="text-foreground">{format.number(info.getValue())}</span>,
      }),
    ]);
  }, [t, format]);
  return (
    <DataCard
      testId="admin-diagnostics"
      footer={
        <p className="py-1 text-xs leading-4 text-fg-subtle tabular-nums" data-testid="diagnostics-total">
          {t("total", { tables: rows.length, rows: total })}
        </p>
      }
    >
      <DataTable
        data={rows}
        columns={columns}
        caption={t("caption")}
        getRowId={(row) => row.table}
        empty={t("empty")}
        testId="diagnostics-table"
        mobile={(row) => ({
          title: row.table,
          titleClassName: "font-mono font-normal",
          status: <span className="text-sm text-foreground tabular-nums">{format.number(row.rows)}</span>,
          meta: t(`areas.${row.area}`),
          data: { table: row.table },
        })}
      />
    </DataCard>
  );
}

/**
 * /admin/diagnostics: the rows of every hub table from GET /v1/admin/stats, by their database names, with the area of
 * the hub each belongs to. For troubleshooting; what an admin acts on is on the overview, which links here.
 */
export function AdminDiagnosticsPage({ initialError }: { initialError: ApiErrorInfo | null }) {
  const t = useTranslations("admin.diagnostics");
  const state = useHubQuery(adminStatsQuery(browserApi), initialError);
  return (
    <>
      <PageHeader title={t("title")} />
      <QueryView state={state} loading={<TableSkeleton rows={10} />}>
        {(stats) => <TableCounts stats={stats} />}
      </QueryView>
    </>
  );
}
