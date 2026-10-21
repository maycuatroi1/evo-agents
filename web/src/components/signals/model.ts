import { MAX_TENANT_CHARS, SIGNAL_KEY_NAME, type Signal, type SignalsFilter } from "@/lib/signal-queries";

/**
 * The Signals page's model: the spans of time a series is read over, the filter of the read as the address names it
 * (`?key=&tenant=&days=`), and a series turned into the points a chart draws, oldest first.
 */

/** The spans a series is read over, in days; 90 is the value check's default window. */
export const RANGES = [7, 30, 90, 365] as const;
export type Range = (typeof RANGES)[number];
export const DEFAULT_RANGE: Range = 90;
/** The signals a series reads at most: the API's own most, the newest first. */
export const SERIES_LIMIT = 1_000;

/** The chart's height, which its skeleton keeps so nothing moves when Recharts arrives. */
export const SERIES_HEIGHT = 240;

export type SignalsQuery = { key: string | null; tenant: string | null; days: Range };

function first(value: string | string[] | undefined | null): string | null {
  const found = Array.isArray(value) ? value[0] : value;
  return found ? found : null;
}

/** The page's filter from its address; a key or tenant the hub would refuse reads as none. */
export function queryOf(params: Record<string, string | string[] | undefined>): SignalsQuery {
  const key = first(params.key);
  const tenant = first(params.tenant);
  const days = Number(first(params.days));
  return {
    key: key && SIGNAL_KEY_NAME.test(key) ? key : null,
    tenant: tenant && tenant.length <= MAX_TENANT_CHARS ? tenant : null,
    days: (RANGES as readonly number[]).includes(days) ? (days as Range) : DEFAULT_RANGE,
  };
}

/**
 * The start of the span: midnight UTC `days` days before today, so the server and the browser ask the same read (one
 * query key) all day long.
 */
export function sinceOf(days: number, now: Date = new Date()): string {
  const start = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate()));
  start.setUTCDate(start.getUTCDate() - days);
  return start.toISOString();
}

/** The read of a key's series in the span the page shows. */
export function seriesFilter(query: SignalsQuery, now: Date = new Date()): SignalsFilter {
  return { key: query.key, tenant: query.tenant, since: sinceOf(query.days, now), limit: SERIES_LIMIT };
}

/** The read of a key's latest signal, of any time and tenant. */
export function latestFilter(key: string): SignalsFilter {
  return { key, tenant: null, since: null, limit: 1 };
}

/** The page's address for a filter; the defaults are left out. */
export function searchOf(query: SignalsQuery): string {
  const search = new URLSearchParams();
  if (query.key) search.set("key", query.key);
  if (query.tenant) search.set("tenant", query.tenant);
  if (query.days !== DEFAULT_RANGE) search.set("days", String(query.days));
  const text = search.toString();
  return text ? `?${text}` : "";
}

export type Point = { at: number; time: string; value: number; tenant: string | null; synthetic: boolean };

/** The numbers of a series as a chart's points, oldest first; feedback and values missing are left out. */
export function pointsOf(signals: readonly Signal[]): Point[] {
  return signals
    .filter((signal) => signal.value !== null)
    .map((signal) => ({ at: Date.parse(signal.measured_at), time: signal.measured_at, value: signal.value as number, tenant: signal.tenant, synthetic: signal.synthetic }))
    .sort((a, b) => a.at - b.at);
}
