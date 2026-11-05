/**
 * The entities view's bounds and its URL parameters, apart from ./entity-graph so the explorer reads `?depth=` and
 * `?limit=` without loading graphology on its first load.
 */

/** The entities the default view and a focus ask for, and the bounds the hub keeps (a3). */
export const DEFAULT_LIMIT = 150;
export const LIMITS = [50, 150, 300, 600, 1000] as const;
export const DEPTHS = [1, 2, 3] as const;
export type Depth = (typeof DEPTHS)[number];
/** The neighbours a double click opens around one entity. */
export const EXPAND_LIMIT = 40;

export function parseLimit(value: string | null | undefined): number {
  const found = Number(value);
  return LIMITS.includes(found as (typeof LIMITS)[number]) ? found : DEFAULT_LIMIT;
}

export function parseDepth(value: string | null | undefined): Depth {
  const found = Number(value);
  return found === 2 || found === 3 ? found : 1;
}
