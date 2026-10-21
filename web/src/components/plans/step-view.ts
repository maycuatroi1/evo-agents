"use client";

import { usePathname, useSearchParams } from "next/navigation";
import { useCallback, useSyncExternalStore } from "react";

/**
 * How the Steps section of a plan's page shows the steps: by status (Board, the default), in plan order (List) or as
 * the graph of their dependencies (Graph). The choice is the URL's `view` (replaced, so Back leaves the page rather
 * than undoing a view) and is kept in localStorage for the next plan opened without one. A `view` the page does not
 * know, or storage that holds junk or refuses to be read, falls back to Board without an error.
 *
 * The step selected on the graph is the URL's `step` (its key, `step=11`), replaced too, so a link to it opens the
 * graph with that step selected; a `step` with no `view` means the graph. Leaving the graph drops `step`.
 */
export const STEP_VIEWS = ["board", "list", "graph"] as const;
export type StepView = (typeof STEP_VIEWS)[number];

const KEY = "hub.plans.stepView";

const STEP = "step";

/** Writes `change` into the current URL's query without a history entry. */
function replaceQuery(pathname: string, change: (query: URLSearchParams) => void) {
  const query = new URLSearchParams(window.location.search);
  change(query);
  const search = query.toString();
  window.history.replaceState(null, "", `${pathname}${search ? `?${search}` : ""}${window.location.hash}`);
}

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
  const fallback = params.get(STEP) ? "graph" : (stored ?? "board");
  const view: StepView = asked === null ? fallback : isView(asked) ? asked : "board";
  const set = (next: StepView) => {
    try {
      window.localStorage.setItem(KEY, next);
    } catch {
      // storage refused: the URL still holds the choice
    }
    replaceQuery(pathname, (query) => {
      query.set("view", next);
      if (next !== "graph") query.delete(STEP);
    });
  };
  return [view, set];
}

/** The key of the step selected on the graph (the URL's `step`), and a setter; null clears it. */
export function useSelectedStep(): [string | null, (key: string | null) => void] {
  const params = useSearchParams();
  const pathname = usePathname();
  const set = useCallback(
    (key: string | null) =>
      replaceQuery(pathname, (query) => {
        if (key === null) query.delete(STEP);
        else query.set(STEP, key);
      }),
    [pathname],
  );
  return [params.get(STEP) || null, set];
}
