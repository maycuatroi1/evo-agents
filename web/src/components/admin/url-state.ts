"use client";

import { usePathname, useSearchParams } from "next/navigation";
import { useCallback, useMemo, useState } from "react";

import { type ParamSource, searchOf } from "./data";

/**
 * A view whose filters live in the URL. Applying filters pushes a history entry (Back returns to the previous
 * filters); turning a page only replaces the URL, so Back leaves the list instead of stepping through its pages.
 * Both go through the History API, which Next.js syncs with useSearchParams, so no server round trip happens: the
 * client queries fetch what the new URL names.
 */
export function useUrlView<T>(parse: (source: ParamSource) => T) {
  const params = useSearchParams();
  const pathname = usePathname();
  const view = useMemo(() => parse(params), [params, parse]);
  const go = useCallback(
    (values: Record<string, string | number>, mode: "push" | "replace") => {
      const url = `${pathname}${searchOf(values)}`;
      if (mode === "push") window.history.pushState(null, "", url);
      else window.history.replaceState(null, "", url);
    },
    [pathname],
  );
  return { view, go };
}

/**
 * The cursors of the pages before the current one, for "previous page". The API only pages forward, so the way back
 * is remembered here, per set of filters; a page opened straight from a link knows no way back and offers the first
 * page instead.
 */
export function useCursorTrail(filterKey: string, cursor: string) {
  const [trail, setTrail] = useState<{ key: string; cursors: string[] }>({ key: filterKey, cursors: [] });
  const cursors = useMemo(
    () => (trail.key === filterKey && cursor !== "" ? trail.cursors : []),
    [trail, filterKey, cursor],
  );
  const page = cursor === "" ? 1 : cursors.length > 0 ? cursors.length + 1 : null;
  const forward = useCallback(() => setTrail({ key: filterKey, cursors: [...cursors, cursor] }), [filterKey, cursors, cursor]);
  const back = useCallback((): string => {
    setTrail({ key: filterKey, cursors: cursors.slice(0, -1) });
    return cursors.at(-1) ?? "";
  }, [filterKey, cursors]);
  return { page, canGoBack: cursors.length > 0, forward, back };
}
