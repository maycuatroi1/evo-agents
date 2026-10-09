import * as React from "react";

/** Whether the browser can answer media queries (jsdom in unit tests cannot). */
function canMatch(): boolean {
  return typeof window !== "undefined" && typeof window.matchMedia === "function";
}

/**
 * Whether `query` matches now, following its changes. The server cannot measure the viewport, so the page it renders,
 * and the first render in the browser that hydrates it, take `serverValue`; right after hydration the browser's own
 * answer takes over. For what CSS cannot say alone, such as whether a column is a scroll region a key can reach.
 */
export function useMediaQuery(query: string, serverValue = false): boolean {
  const subscribe = React.useCallback(
    (onChange: () => void) => {
      if (!canMatch()) return () => {};
      const mql = window.matchMedia(query);
      mql.addEventListener("change", onChange);
      return () => mql.removeEventListener("change", onChange);
    },
    [query],
  );
  const snapshot = React.useCallback(() => canMatch() && window.matchMedia(query).matches, [query]);
  return React.useSyncExternalStore(subscribe, snapshot, () => serverValue);
}

/** Tailwind's xl breakpoint (80rem, 1280 px), where the run page splits into its session and its side column. */
export const XL_QUERY = "(min-width: 80rem)";
