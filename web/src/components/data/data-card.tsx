"use client";

import { createContext, type ReactNode, useContext } from "react";

import { cn } from "@/lib/utils";

const InCard = createContext(false);

/**
 * Whether a table, a state or a skeleton renders inside a `DataCard`, which draws the frame: they then draw none of
 * their own, so a list is one card and never a card inside a card.
 */
export function useInCard(): boolean {
  return useContext(InCard);
}

/**
 * The card a list lives in (the kit's DataTable): the toolbar on top, then the table, or the empty, no-results or
 * loading state in its place, then an optional footer such as the pager. A footer that renders nothing takes no room.
 */
export function DataCard({
  toolbar,
  footer,
  children,
  busy = false,
  className,
  testId,
}: {
  toolbar?: ReactNode;
  footer?: ReactNode;
  children: ReactNode;
  /** The rows shown are the previous page's while the next one loads. */
  busy?: boolean;
  className?: string;
  testId?: string;
}) {
  return (
    <div
      className={cn("flex min-w-0 flex-col overflow-hidden rounded-md border bg-card shadow-raised", className)}
      data-testid={testId}
    >
      <InCard.Provider value>
        {toolbar}
        <div aria-busy={busy || undefined} className={cn("min-w-0 transition-opacity", busy && "opacity-60")}>
          {children}
        </div>
        {footer ? <div className="border-t px-4 py-2 empty:hidden">{footer}</div> : null}
      </InCard.Provider>
    </div>
  );
}

/**
 * The toolbar at the top of a list's card: the search, the filter chips with their counts, then the number of
 * results on the right, said again in a polite live region when it changes. It is the list's search landmark.
 */
export function DataToolbar({
  label,
  children,
  count,
  countTestId,
}: {
  /** The landmark's name: what the list is ("Runs of project evo-agents"). */
  label: string;
  children: ReactNode;
  count?: ReactNode;
  countTestId?: string;
}) {
  return (
    <div role="search" aria-label={label} className="flex flex-wrap items-center gap-x-3 gap-y-2 border-b px-4 py-3">
      {children}
      {count !== undefined && count !== null ? (
        <p
          aria-live="polite"
          className="ml-auto text-xs leading-4 text-fg-subtle tabular-nums"
          data-testid={countTestId}
        >
          {count}
        </p>
      ) : null}
    </div>
  );
}
