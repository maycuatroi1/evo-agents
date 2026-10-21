/**
 * What the worker on duty last said of keeping the project's graph fresh, as the night's figures keep it (`kg_sync`,
 * evo_agents.hub.server.collect): read apart from the graph's health, since the Curator's tabs read it on every page to
 * mark Graph health while a sync failed. Read defensively: a value missing or of another type is null.
 */

function num(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function text(value: unknown): string | null {
  return typeof value === "string" && value !== "" ? value : null;
}

function record(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

export type SyncSource = { source: string; status: string; error: string | null; commit: string | null; behind: number | null; checkedAt: string | null };
export type SyncState = { worker: string | null; checkedAt: string | null; error: string | null; failed: number; sources: SyncSource[] };

/** What the worker on duty last said of keeping the graph fresh (the figures' `kg_sync`); null when it said nothing. */
export function syncOf(value: unknown): SyncState | null {
  if (typeof value !== "object" || value === null) return null;
  const found = record(value);
  const sources = (Array.isArray(found.sources) ? found.sources : []).map((raw) => {
    const entry = record(raw);
    return {
      source: text(entry.source) ?? "-",
      status: text(entry.status) ?? "ok",
      error: text(entry.error),
      commit: text(entry.commit),
      behind: num(entry.behind),
      checkedAt: text(entry.checked_at),
    };
  });
  return { worker: text(found.worker), checkedAt: text(found.checked_at), error: text(found.error), failed: Math.max(0, num(found.failed) ?? 0), sources };
}
