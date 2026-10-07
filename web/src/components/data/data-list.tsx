"use client";

import type { Route } from "next";
import Link from "next/link";
import { type ReactNode, useId } from "react";

import { cn } from "@/lib/utils";

/**
 * One row of a list as a phone shows it (the kit's DataTable under 768 px): the title, its state at the end of the
 * line, and one meta line under it. What else a table row holds is on the object's own page, which the row opens.
 */
export type DataListRow = {
  /** The object's name, on one line, cut with an ellipsis. */
  title: ReactNode;
  /** The object's page: the title links there, and the link's target covers the whole row. */
  href?: Route;
  /** Classes for the title, such as `font-mono` for a name that is an identifier. */
  titleClassName?: string;
  /** The title's tooltip, when it is cut. */
  titleText?: string;
  /** Tags right after the title: Plan run, Hub admin, Current session. */
  tags?: ReactNode;
  /** The row's state at the end of its first line: a StatusBadge, or another short value such as a time. */
  status?: ReactNode;
  /** One line under the title, cut with an ellipsis; `metaText` is its whole text, as a tooltip. */
  meta?: ReactNode;
  metaText?: string;
  /** The meta line says why something failed: `danger`. */
  danger?: boolean;
  /** The row's buttons, at its end, always shown. */
  actions?: ReactNode;
  /** `data-*` attributes of the row, for scripts and tests (`{ "run-id": 12 }`). */
  data?: Record<string, string | number | undefined>;
};

function dataAttributes(data: DataListRow["data"]) {
  return Object.fromEntries(Object.entries(data ?? {}).map(([name, value]) => [`data-${name}`, value]));
}

function DataListItem({ row, compact }: { row: DataListRow; compact: boolean }) {
  const ids = useId();
  const described = [row.status ? `${ids}-status` : "", row.meta ? `${ids}-meta` : ""].filter(Boolean).join(" ") || undefined;
  const title = cn("min-w-0 truncate text-sm leading-5 font-medium text-foreground", row.titleClassName);
  return (
    <li
      className={cn(
        "relative flex items-center gap-3 px-4",
        compact ? "min-h-[52px] py-2" : "min-h-[60px] py-2.5",
        // The row's own link covers it; every other link or button in it sits above that cover, so it stays its own.
        "[&_:is(a,button):not([data-row-link])]:relative [&_:is(a,button):not([data-row-link])]:z-[1]",
        row.href && "transition-colors hover:bg-accent active:bg-accent",
      )}
      data-slot="data-list-row"
      {...dataAttributes(row.data)}
    >
      <div className="flex min-w-0 flex-1 flex-col gap-0.5">
        <div className="flex min-w-0 items-center gap-2">
          {row.href ? (
            <Link
              href={row.href}
              title={row.titleText}
              aria-describedby={described}
              data-row-link=""
              className={cn(
                title,
                "after:absolute after:inset-0 after:content-[''] focus-visible:outline-none",
                "focus-visible:after:rounded-xs focus-visible:after:outline-2 focus-visible:after:-outline-offset-2 focus-visible:after:outline-ring",
              )}
            >
              {row.title}
            </Link>
          ) : (
            <span className={title} title={row.titleText}>
              {row.title}
            </span>
          )}
          {row.tags ? <span className="flex shrink-0 items-center gap-1.5 empty:hidden">{row.tags}</span> : null}
          {row.status ? (
            <span id={`${ids}-status`} className="ml-auto flex shrink-0 items-center pl-1">
              {row.status}
            </span>
          ) : null}
        </div>
        {row.meta ? (
          <p
            id={`${ids}-meta`}
            className={cn("truncate text-xs leading-4", row.danger ? "text-danger" : "text-fg-subtle")}
            title={row.metaText}
          >
            {row.meta}
          </p>
        ) : null}
      </div>
      {row.actions ? <div className="flex shrink-0 items-center gap-1">{row.actions}</div> : null}
    </li>
  );
}

/**
 * The rows of a table as a list, for screens under 768 px: rows of at least 60 px (52 px `compact`) whose title links
 * to the object's page with a target as large as the row, divided by hairlines. A named list, so a screen reader says
 * what it lists and how many rows it holds; each link is described by the row's state and meta line.
 */
export function DataList({
  label,
  rows,
  compact = false,
  empty,
}: {
  label: string;
  rows: { id: string; row: DataListRow }[];
  compact?: boolean;
  empty?: ReactNode;
}) {
  if (rows.length === 0 && empty) {
    return <p className="px-4 py-6 text-center text-[13px] text-muted-foreground">{empty}</p>;
  }
  return (
    // role="list": Safari drops the list role of a list styled without markers.
    <ul role="list" aria-label={label} className="flex flex-col divide-y">
      {rows.map(({ id, row }) => (
        <DataListItem key={id} row={row} compact={compact} />
      ))}
    </ul>
  );
}
