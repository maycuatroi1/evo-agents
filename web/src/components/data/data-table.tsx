"use client";

import {
  createColumnHelper,
  createSortedRowModel,
  type RowData,
  rowSortingFeature,
  sortFn_alphanumeric,
  sortFn_basic,
  sortFn_datetime,
  sortFn_text,
  type SortingState,
  type TableOptions,
  tableFeatures,
  useTable,
} from "@tanstack/react-table";
import { ArrowDown, ArrowUp, ArrowUpDown } from "lucide-react";
import { useTranslations } from "next-intl";
import type { ReactNode } from "react";

import { Table, TableBody, TableCaption, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { cn } from "@/lib/utils";

/** The table features every data table of the hub registers: client-side sorting, nothing else yet. */
export const dataTableFeatures = tableFeatures({
  rowSortingFeature,
  sortedRowModel: createSortedRowModel(),
  sortFns: { alphanumeric: sortFn_alphanumeric, text: sortFn_text, datetime: sortFn_datetime, basic: sortFn_basic },
});

export type DataTableFeatures = typeof dataTableFeatures;

/** Build columns with `dataTableColumns<Row>().columns([...])`, which keeps each column's value type. */
export function dataTableColumns<TData extends RowData>() {
  return createColumnHelper<DataTableFeatures, TData>();
}

type DataTableProps<TData extends RowData> = {
  data: TData[];
  columns: TableOptions<DataTableFeatures, TData>["columns"];
  caption: string;
  getRowId?: (row: TData) => string;
  initialSorting?: SortingState;
  empty?: ReactNode;
  testId?: string;
  columnClassNames?: Record<string, string>;
};

/**
 * A sortable, accessible table: real <table> markup with a caption, aria-sort on sorted headers, and sort buttons
 * reachable by keyboard. Narrow screens hide the columns listed in `columnClassNames` (e.g. "hidden md:table-cell").
 */
export function DataTable<TData extends RowData>({
  data,
  columns,
  caption,
  getRowId,
  initialSorting = [],
  empty,
  testId,
  columnClassNames = {},
}: DataTableProps<TData>) {
  const t = useTranslations("table");
  const table = useTable({
    features: dataTableFeatures,
    columns,
    data,
    getRowId: getRowId ? (row) => getRowId(row) : undefined,
    initialState: { sorting: initialSorting },
    enableSortingRemoval: false,
  });

  const rows = table.getRowModel().rows;
  return (
    <div className="overflow-hidden rounded-xl border bg-card" data-testid={testId}>
      <Table scrollLabel={caption}>
        <TableCaption className="sr-only">{caption}</TableCaption>
        <TableHeader className="bg-muted/50">
          {table.getHeaderGroups().map((group) => (
            <TableRow key={group.id} className="hover:bg-transparent">
              {group.headers.map((header) => {
                const sorted = header.column.getIsSorted();
                const canSort = header.column.getCanSort();
                return (
                  <TableHead
                    key={header.id}
                    aria-sort={sorted === "asc" ? "ascending" : sorted === "desc" ? "descending" : undefined}
                    className={cn("h-10 px-3 text-xs font-medium text-muted-foreground", columnClassNames[header.column.id])}
                  >
                    {header.isPlaceholder ? null : canSort ? (
                      <button
                        type="button"
                        onClick={header.column.getToggleSortingHandler()}
                        className="-mx-1.5 inline-flex items-center gap-1.5 rounded-md px-1.5 py-1 transition-colors hover:bg-muted hover:text-foreground"
                      >
                        <table.FlexRender header={header} />
                        {sorted === "asc" ? (
                          <ArrowUp className="size-3.5" aria-hidden="true" />
                        ) : sorted === "desc" ? (
                          <ArrowDown className="size-3.5" aria-hidden="true" />
                        ) : (
                          <ArrowUpDown className="size-3.5 opacity-60" aria-hidden="true" />
                        )}
                        <span className="sr-only">
                          {sorted === "asc" ? t("sortAscending") : sorted === "desc" ? t("sortDescending") : ""}
                        </span>
                      </button>
                    ) : (
                      <table.FlexRender header={header} />
                    )}
                  </TableHead>
                );
              })}
            </TableRow>
          ))}
        </TableHeader>
        <TableBody>
          {rows.length === 0 && empty ? (
            <TableRow>
              <TableCell colSpan={table.getAllLeafColumns().length} className="h-24 text-center text-muted-foreground">
                {empty}
              </TableCell>
            </TableRow>
          ) : (
            rows.map((row) => (
              <TableRow key={row.id}>
                {row.getAllCells().map((cell) => (
                  <TableCell key={cell.id} className={cn("px-3 py-2.5", columnClassNames[cell.column.id])}>
                    <table.FlexRender cell={cell} />
                  </TableCell>
                ))}
              </TableRow>
            ))
          )}
        </TableBody>
      </Table>
    </div>
  );
}
