"use client";

import { Identifier } from "@/components/data/identifier";
import { cn } from "@/lib/utils";

/** How many chips a compact list shows before "+N". */
const COMPACT_CHIPS = 2;

/**
 * Names in a list of identifier chips: a worker's runtimes, checkouts, projects and labels. `compact`, for table
 * cells, keeps them on one line and sets the empty text at the chips' size.
 */
export function ChipList({
  items,
  empty,
  compact = false,
  label,
}: {
  items: string[];
  empty: string;
  /** In a table's row: one line, the first two chips and "+N" for the rest, which a tooltip and screen readers name. */
  compact?: boolean;
  label?: string;
}) {
  if (items.length === 0) return <span className={cn("text-muted-foreground", compact ? "text-xs" : "text-sm")}>{empty}</span>;
  const shown = compact ? items.slice(0, COMPACT_CHIPS) : items;
  const rest = items.slice(shown.length);
  return (
    <ul className={cn("flex gap-1", compact ? "flex-nowrap items-center" : "flex-wrap")} aria-label={label}>
      {shown.map((item) => (
        <li key={item} className="max-w-full min-w-0">
          <Identifier value={item} />
        </li>
      ))}
      {rest.length > 0 ? (
        <li className="shrink-0 text-xs text-fg-subtle tabular-nums" title={rest.join(", ")}>
          <span aria-hidden="true">+{rest.length}</span>
          <span className="sr-only">{rest.join(", ")}</span>
        </li>
      ) : null}
    </ul>
  );
}
