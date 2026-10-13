"use client";

import { usePathname, useSearchParams } from "next/navigation";
import { useSyncExternalStore } from "react";

/**
 * How the Steps section of a plan's page shows the steps: by status (Board, the default), in plan order (List) or as
 * the graph of their dependencies (Graph). The choice is the URL's `view` (replaced, so Back leaves the page rather
 * than undoing a view) and is kept in localStorage for the next plan opened without one. A `view` the page does not
 * know, or storage that holds junk or refuses to be read, falls back to Board without an error.
 */
export const STEP_VIEWS = ["board", "list", "graph"] as const;
export type StepView = (typeof STEP_VIEWS)[number];

const KEY = "hub.plans.stepView";

const isView = (value: string | null): value is StepView => STEP_VIEWS.includes(value as StepView);

function saved(): StepView | null {
  try {
    const value = window.localStorage.getItem(KEY);
    return isView(value) ? value : null;
  } catch {
    return null; // storage refused (a private window, blocked site data)
  }
}

// Another tab chose; this tab's choice re-renders through the URL, which holds it too.
function subscribe(listener: () => void) {
  window.addEventListener("storage", listener);
  return () => window.removeEventListener("storage", listener);
}

export function useStepView(): [StepView, (next: StepView) => void] {
  const params = useSearchParams();
  const pathname = usePathname();
  const stored = useSyncExternalStore(subscribe, saved, () => null);
  const asked = params.get("view");
  const view: StepView = asked === null ? (stored ?? "board") : isView(asked) ? asked : "board";
  const set = (next: StepView) => {
    try {
      window.localStorage.setItem(KEY, next);
    } catch {
      // storage refused: the URL still holds the choice
    }
    const query = new URLSearchParams(window.location.search);
    query.set("view", next);
    window.history.replaceState(null, "", `${pathname}?${query}${window.location.hash}`);
  };
  return [view, set];
}
