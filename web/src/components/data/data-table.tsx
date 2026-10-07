"use client";

import {
  createColumnHelper,
  createSortedRowModel,
  metaHelper,
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
import { ChevronDown, ChevronsUpDown, ChevronUp } from "lucide-react";
import { useTranslations } from "next-intl";
import type { ReactNode } from "react";

import { Table, TableBody, TableCaption, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { useIsMobile } from "@/hooks/use-mobile";
import { cn } from "@/lib/utils";

import { useInCard } from "./data-card";
import { DataList, type DataListRow } from "./data-list";

/** What a column says about itself to the table (`meta` of a column definition). */
export type DataColumnMeta = {
  /** The row's title cell (`CellMain`): it takes the width the other columns leave and cuts its lines to fit. */
  primary?: boolean;
  /** A number, size, duration or time: right-aligned in tabular figures, its header too. */
  numeric?: boolean;
  /** The row's actions: shown while the row is hovered or holds focus, wherever a pointer can hover. */
  actions?: boolean;
};

/** The table features every data table of the hub registers: client-side sorting and the column meta above. */
export const dataTableFeatures = tableFeatures({
  rowSortingFeature,
  sortedRowModel: createSortedRowModel(),
  sortFns: { alphanumeric: sortFn_alphanumeric, text: sortFn_text, datetime: sortFn_datetime, basic: sortFn_basic },
  columnMeta: metaHelper<DataColumnMeta>(),
});

export type DataTableFeatures = typeof dataTableFeatures;

/** Build columns with `dataTableColumns<Row>().columns([...])`, which keeps each column's value type. */
export function dataTableColumns<TData extends RowData>() {
  return createColumnHelper<DataTableFeatures, TData>();
}

/** Rows of 44 px, or 36 px for long logs such as the audit trail. */
export type Density = "comfortable" | "compact";

type DataTableProps<TData extends RowData> = {
  data: TData[];
  columns: TableOptions<DataTableFeatures, TData>["columns"];
  caption: string;
  getRowId?: (row: TData) => string;
  initialSorting?: SortingState;
  /** One line in place of the rows when there are none, such as "No plan matches the search." */
  empty?: ReactNode;
  density?: Density;
  testId?: string;
  columnClassNames?: Record<string, string>;
  /**
   * The row as a phone lists it: under 768 px the table becomes a list of these (title, state, one meta line), each
   * opening the object's page, where the other columns are. Without it the table stays a table at every width.
   */
  mobile?: (row: TData) => DataListRow;
};

/**
 * The primary cell of a row: the object's title on one line in `body-strong` (a link in `NAME_LINK` with
 * `truncate`, and tags after it), then at most one secondary line in `caption`, `fg-subtle`, or `danger` for a
 * failure. Both lines end in an ellipsis instead of wrapping, so a row never grows past two lines; `subTitle` gives
 * the whole secondary line as a tooltip. Use it in the column marked `primary`.
 */
export function CellMain({
  children,
  sub,
  subTitle,
  danger = false,
  subTestId,
  className,
}: {
  children: ReactNode;
  sub?: ReactNode;
  subTitle?: string;
  danger?: boolean;
  subTestId?: string;
  className?: string;
}) {
  return (
    // w-0 with min-w-full: the cell adds no width of its own to the table, then fills what its column gets.
    <div className={cn("flex w-0 min-w-full flex-col gap-px", className)}>
      <div className="flex min-w-0 items-center gap-2 text-sm leading-5 font-medium text-foreground">{children}</div>
      {sub ? (
        <span
          className={cn("truncate text-xs leading-4", danger ? "text-danger" : "text-fg-subtle")}
          title={subTitle}
          data-testid={subTestId}
        >
          {sub}
        </span>
      ) : null}
    </div>
  );
}

/**
 * The kit's table, accessible: real <table> markup with a caption, aria-sort on sorted headers and sort buttons
 * reachable by keyboard. The header sits on `surface-sunken` in 12 px `fg-muted`; rows are 44 px (36 px `compact`)
 * with cells in `fg-muted` and the title cell in `fg`; numbers line up on the right in tabular figures; the actions
 * of a row show while it is hovered or holds focus. Inside a `DataCard` the table draws no frame of its own; alone,
 * it is a card. Narrow screens hide the columns listed in `columnClassNames` (e.g. "hidden md:table-cell"); with
 * `mobile`, screens under 768 px get a list of rows instead (`DataList`), in the table's order, under the same test id
 * with `data-layout="list"`.
 */
export function DataTable<TData extends RowData>({
  data,
  columns,
  caption,
  getRowId,
  initialSorting = [],
  empty,
  density = "comfortable",
  testId,
  columnClassNames = {},
  mobile,
}: DataTableProps<TData>) {
  const t = useTranslations("table");
  const inCard = useInCard();
  const phone = useIsMobile();
  const table = useTable({
    features: dataTableFeatures,
    columns,
    data,
    getRowId: getRowId ? (row) => getRowId(row) : undefined,
    initialState: { sorting: initialSorting },
    enableSortingRemoval: false,
  });

  const rows = table.getRowModel().rows;
  const compact = density === "compact";
  const frame = cn(!inCard && "overflow-hidden rounded-md border bg-card shadow-raised");
  if (mobile && phone) {
    return (
      <div className={frame} data-testid={testId} data-density={density} data-layout="list">
        <DataList
          label={caption}
          rows={rows.map((row) => ({ id: row.id, row: mobile(row.original) }))}
          compact={compact}
          empty={empty}
        />
      </div>
    );
  }
  return (
    <div className={frame} data-testid={testId} data-density={density} data-layout="table">
      <Table scrollLabel={caption} className="text-[13px] leading-[18px]">
        <TableCaption className="sr-only">{caption}</TableCaption>
        <TableHeader>
          {table.getHeaderGroups().map((group) => (
            <TableRow key={group.id} className="hover:bg-transparent">
              {group.headers.map((header) => {
                const sorted = header.column.getIsSorted();
                const canSort = header.column.getCanSort();
                const meta = header.column.columnDef.meta;
                return (
                  <TableHead
                    key={header.id}
                    aria-sort={sorted === "asc" ? "ascending" : sorted === "desc" ? "descending" : undefined}
                    className={cn(
                      "h-9 bg-surface-sunken px-3 text-xs font-medium text-muted-foreground first:pl-4 last:pr-4",
                      sorted && "text-foreground",
                      meta?.primary && "w-full min-w-40",
                      (meta?.numeric || meta?.actions) && "text-right",
                      columnClassNames[header.column.id],
                    )}
                  >
                    {header.isPlaceholder ? null : canSort ? (
                      <button
                        type="button"
                        onClick={header.column.getToggleSortingHandler()}
                        className="group/sort -mx-1 inline-flex cursor-pointer items-center gap-1 rounded-xs px-1 py-0.5 transition-colors hover:text-foreground max-md:min-h-11"
                      >
                        <table.FlexRender header={header} />
                        {sorted === "asc" ? (
                          <ChevronUp className="size-3.5" aria-hidden="true" />
                        ) : sorted === "desc" ? (
                          <ChevronDown className="size-3.5" aria-hidden="true" />
                        ) : (
                          <ChevronsUpDown
                            className="size-3.5 opacity-0 transition-opacity group-hover/sort:opacity-100 group-focus-visible/sort:opacity-100"
                            aria-hidden="true"
                          />
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
            <TableRow className="hover:bg-transparent">
              <TableCell
                colSpan={table.getAllLeafColumns().length}
                className="h-16 px-4 text-center whitespace-normal text-muted-foreground"
              >
                {empty}
              </TableCell>
            </TableRow>
          ) : (
            rows.map((row) => (
              <TableRow key={row.id}>
                {row.getAllCells().map((cell) => {
                  const meta = cell.column.columnDef.meta;
                  return (
                    <TableCell
                      key={cell.id}
                      className={cn(
                        "px-3 text-muted-foreground first:pl-4 last:pr-4",
                        compact ? "h-9 py-1" : "h-11 py-1.5",
                        meta?.primary && "min-w-40",
                        meta?.numeric && "text-right tabular-nums",
                        meta?.actions && "w-px",
                        columnClassNames[cell.column.id],
                      )}
                    >
                      {meta?.actions ? (
                        <div className="row-actions flex items-center justify-end gap-1">
                          <table.FlexRender cell={cell} />
                        </div>
                      ) : (
                        <table.FlexRender cell={cell} />
                      )}
                    </TableCell>
                  );
                })}
              </TableRow>
            ))
          )}
        </TableBody>
      </Table>
    </div>
  );
}
