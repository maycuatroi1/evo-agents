"use client";

import { Identifier } from "@/components/data/identifier";
import { cn } from "@/lib/utils";

/**
 * Names in a list of identifier chips: a worker's runtimes, checkouts, projects and labels. `compact` sets the empty
 * text at the chips' size, for table cells.
 */
export function ChipList({
  items,
  empty,
  compact = false,
  label,
}: {
  items: string[];
  empty: string;
  compact?: boolean;
  label?: string;
}) {
  if (items.length === 0) return <span className={cn("text-muted-foreground", compact ? "text-xs" : "text-sm")}>{empty}</span>;
  return (
    <ul className="flex flex-wrap gap-1" aria-label={label}>
      {items.map((item) => (
        <li key={item} className="max-w-full">
          <Identifier value={item} />
        </li>
      ))}
    </ul>
  );
}
