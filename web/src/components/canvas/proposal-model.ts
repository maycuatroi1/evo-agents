import type { CanvasProposal, CanvasTestCard, Cell, Confidence } from "./api";
import { CELLS, CONFIDENCES, valueText } from "./model";

/**
 * The pure model of the proposals that wait on a canvas: a new claim read from the fields it sets, and the proposals
 * one writer made for one reason gathered so the page says the reason once.
 */
const TEST_CARD_FIELDS = [
  "hypothesis",
  "test",
  "metric",
  "aggregate",
  "window_days",
  "comparator",
  "threshold",
  "min_samples",
  "tenant",
  "deadline",
] as const satisfies readonly (keyof CanvasTestCard)[];

/** A test card a proposal sets, with every field the pages read (null where it sets none); null when it sets nothing. */
export function testCardFrom(value: unknown): CanvasTestCard | null {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return null;
  const source = value as Record<string, unknown>;
  const card = Object.fromEntries(TEST_CARD_FIELDS.map((field) => [field, source[field] ?? null])) as CanvasTestCard;
  return Object.values(card).some((field) => field !== null && field !== "") ? card : null;
}

/** What a proposal of a new claim would make, read from the fields it sets as an edit sets them. */
export type ProposedClaim = {
  cell: Cell | null;
  text: string;
  confidence: Confidence | null;
  testCard: CanvasTestCard | null;
  signalKeys: string[];
  useCases: string[];
  problems: string[];
  /** The fields it sets besides those above (a place in the cell, a delivery), each with its value. */
  other: { field: string; value: unknown }[];
};

const PROPOSED_FIELDS = new Set(["cell", "text", "confidence", "test_card", "signal_keys", "use_cases", "problems"]);

function texts(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string" && item !== "") : [];
}

export function proposedClaim(fields: Record<string, unknown>): ProposedClaim {
  return {
    cell: CELLS.find((cell) => cell === fields.cell) ?? null,
    text: typeof fields.text === "string" ? fields.text : "",
    confidence: CONFIDENCES.find((confidence) => confidence === fields.confidence) ?? null,
    testCard: testCardFrom(fields.test_card),
    signalKeys: texts(fields.signal_keys),
    useCases: texts(fields.use_cases),
    problems: texts(fields.problems),
    other: Object.entries(fields)
      .filter(([field, value]) => !PROPOSED_FIELDS.has(field) && valueText(value) !== "")
      .map(([field, value]) => ({ field, value })),
  };
}

/** Proposals that one writer made for one reason (a brainstorm's), so the page says the reason once over them. */
export type ProposalGroup<P> = { key: string; reason: string; proposals: P[] };

/**
 * The proposals in the order the hub lists them, those that share their reason and their writer (the same agent run,
 * or the same person) gathered at the first of them; a proposal without a reason stands alone.
 */
export function proposalGroups<P extends Pick<CanvasProposal, "id" | "reason" | "provenance">>(proposals: readonly P[]): ProposalGroup<P>[] {
  const groups = new Map<string, ProposalGroup<P>>();
  for (const proposal of proposals) {
    const { actor, login, run_id: run } = proposal.provenance;
    const reason = proposal.reason.trim();
    const key = reason ? JSON.stringify([reason, actor, login, run]) : `proposal-${proposal.id}`;
    const group = groups.get(key);
    if (group) group.proposals.push(proposal);
    else groups.set(key, { key, reason, proposals: [proposal] });
  }
  return [...groups.values()];
}
