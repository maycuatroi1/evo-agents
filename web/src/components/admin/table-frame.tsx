import type { ReactNode } from "react";

/**
 * Holds a data table inside the page's width. A table scrolls sideways in its own region, but its natural width
 * still counts towards the width of every block around it, up to the shell's <main>, which is a flex item that
 * grows to fit; a one-column grid whose column may shrink to zero stops that, so the page never scrolls sideways.
 */
export function TableFrame({ children }: { children: ReactNode }) {
  return <div className="grid min-w-0 grid-cols-[minmax(0,1fr)]">{children}</div>;
}
