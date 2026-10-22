/**
 * A proposal of kind memory's promotion as the hub keeps it (proposals.promotion, evo_agents.hub.server.memory_promotion):
 * the agents' memories it promotes, each at the revision it read, the shared memory it merges into (by id and revision)
 * or the new one it makes (by location, name and type), the whole memory after the merge, and once accepted the shared
 * memory and revision it wrote. The hub keeps it as JSON; this reads it defensively, null when it is no promotion.
 */

export type PromotionSource = { memoryId: number; revision: number; agent: string | null };

export type PromotionTarget =
  | { kind: "merge"; memoryId: number; revision: number }
  | { kind: "new"; location: string; name: string; type: string };

export type PromotionView = {
  sources: PromotionSource[];
  target: PromotionTarget | null;
  body: string;
  written: { memoryId: number; revision: number } | null;
};

const record = (value: unknown): Record<string, unknown> | null =>
  value !== null && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : null;
const int = (value: unknown): number | null => (typeof value === "number" && Number.isInteger(value) && value > 0 ? value : null);
const str = (value: unknown): string | null => (typeof value === "string" && value !== "" ? value : null);

function target(raw: Record<string, unknown> | null): PromotionTarget | null {
  if (!raw) return null;
  const memoryId = int(raw.memory_id);
  const revision = int(raw.revision);
  if (memoryId !== null && revision !== null) return { kind: "merge", memoryId, revision };
  const location = str(raw.location);
  const name = str(raw.name);
  const type = str(raw.type);
  return location && name && type ? { kind: "new", location, name, type } : null;
}

export function promotionView(raw: unknown): PromotionView | null {
  const promotion = record(raw);
  if (!promotion) return null;
  const sources = (Array.isArray(promotion.sources) ? promotion.sources : []).flatMap((item): PromotionSource[] => {
    const source = record(item);
    const memoryId = int(source?.memory_id);
    const revision = int(source?.revision);
    return source && memoryId !== null && revision !== null ? [{ memoryId, revision, agent: str(source.agent) }] : [];
  });
  const written = record(promotion.written);
  const writtenId = int(written?.memory_id);
  const writtenRevision = int(written?.revision);
  return {
    sources,
    target: target(record(promotion.target)),
    body: typeof promotion.body === "string" ? promotion.body : "",
    written: writtenId !== null && writtenRevision !== null ? { memoryId: writtenId, revision: writtenRevision } : null,
  };
}
