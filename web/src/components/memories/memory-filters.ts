import { isMemoryType, type MemoryType } from "./memory-meta";

/**
 * The facets of a memory list, kept in the URL (?q=, ?location=, ?type=) so a filtered view can be shared and
 * survives a reload. They narrow what the API returned; they never decide what the visitor may see.
 */
export type MemoryFilters = { q: string; location: string | null; type: MemoryType | null };

export const NO_FILTERS: MemoryFilters = { q: "", location: null, type: null };
export const MAX_QUERY = 500; // the API's bound on q

type Params = { get(name: string): string | null };

export function readFilters(params: Params): MemoryFilters {
  const q = (params.get("q") ?? "").trim().slice(0, MAX_QUERY);
  const location = params.get("location")?.trim() || null;
  const type = params.get("type");
  return {
    q,
    location: location && location.length <= 255 ? location : null,
    type: type && isMemoryType(type) ? type : null,
  };
}

/** The search params a server page receives, read as the browser's URLSearchParams would (first value wins). */
export type SearchRecord = Record<string, string | string[] | undefined>;

export function recordParams(record: SearchRecord): Params {
  return {
    get: (name) => {
      const value = record[name];
      return typeof value === "string" ? value : Array.isArray(value) ? (value[0] ?? null) : null;
    },
  };
}

/** The query string of `filters`, without empty values; "" when none is set. */
export function filtersQuery(filters: MemoryFilters): string {
  const params = new URLSearchParams();
  if (filters.q.trim()) params.set("q", filters.q.trim());
  if (filters.location) params.set("location", filters.location);
  if (filters.type) params.set("type", filters.type);
  const text = params.toString();
  return text ? `?${text}` : "";
}

export function isFiltered(filters: MemoryFilters): boolean {
  return Boolean(filters.q || filters.location || filters.type);
}

type Faceted = { location: string; type: string };

export function countBy<T>(items: readonly T[], key: (item: T) => string): Map<string, number> {
  const counts = new Map<string, number>();
  for (const item of items) counts.set(key(item), (counts.get(key(item)) ?? 0) + 1);
  return counts;
}

export function applyFilters<T extends Faceted>(items: readonly T[], filters: MemoryFilters): T[] {
  return items.filter(
    (item) => (!filters.location || item.location === filters.location) && (!filters.type || item.type === filters.type),
  );
}

/**
 * The locations to offer: those the project declares (its harness, then its repos), then any other one a memory
 * sits in, so a memory of a repo since removed can still be found.
 */
export function locationOptions(declared: readonly string[], items: readonly Faceted[]): string[] {
  const seen = new Set(declared);
  const extra = [...new Set(items.map((item) => item.location))].filter((location) => !seen.has(location)).sort();
  return [...declared, ...extra];
}
