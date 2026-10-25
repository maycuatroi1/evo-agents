import { isApiError } from "@/lib/api/errors";

import type {
  AddedEvidenceKind,
  CanvasChange,
  CanvasClaim,
  CanvasContent,
  CanvasTestCard,
  CanvasTestCardIn,
  Cell,
  ClaimStatus,
  Confidence,
  Delivery,
  Direction,
} from "./api";

/**
 * The pure model of the canvas pages: the nine cells in the order Lean Canvas numbers them and where each sits on the
 * printed canvas, the rules a claim's status follows as far as the page can tell before asking the hub, an edit form
 * turned into the change the hub takes, and the claims a 409 says changed since they were read.
 */

/** `tables_canvas.CELLS`: the order Lean Canvas fills its cells in, which is also the order a screen reader hears. */
export const CELLS = [
  "problem",
  "customer_segments",
  "unique_value_proposition",
  "solution",
  "channels",
  "revenue_streams",
  "cost_structure",
  "key_metrics",
  "unfair_advantage",
] as const satisfies readonly Cell[];

/**
 * Where each cell sits on the printed canvas from xl: five columns of two grid tracks each over two rows, Problem, the
 * value proposition and the segments across both, then cost structure and revenue streams side by side under them.
 */
export const CELL_PLACE: Record<Cell, string> = {
  problem: "xl:col-span-2 xl:col-start-1 xl:row-span-2 xl:row-start-1",
  solution: "xl:col-span-2 xl:col-start-3 xl:row-start-1",
  key_metrics: "xl:col-span-2 xl:col-start-3 xl:row-start-2",
  unique_value_proposition: "xl:col-span-2 xl:col-start-5 xl:row-span-2 xl:row-start-1",
  unfair_advantage: "xl:col-span-2 xl:col-start-7 xl:row-start-1",
  channels: "xl:col-span-2 xl:col-start-7 xl:row-start-2",
  customer_segments: "xl:col-span-2 xl:col-start-9 xl:row-span-2 xl:row-start-1",
  cost_structure: "xl:col-span-5 xl:col-start-1 xl:row-start-3",
  revenue_streams: "xl:col-span-5 xl:col-start-6 xl:row-start-3",
};

/** `tables_canvas.STATUSES`, in the order a claim usually moves through them. */
export const STATUSES = ["assumption", "testing", "validated", "invalidated", "retired"] as const satisfies readonly ClaimStatus[];
/** `tables_canvas.DELIVERIES`: code follows the build path, action becomes a task. */
export const DELIVERIES = ["code", "action"] as const satisfies readonly Delivery[];
export const CONFIDENCES = ["low", "medium", "high"] as const satisfies readonly Confidence[];
/** `writes.ADDED_KINDS`: the evidence a person adds; signal and feedback are the hub's to write. */
export const ADDED_KINDS = ["interview", "experiment", "market", "doc", "simulation"] as const satisfies readonly AddedEvidenceKind[];
export const DIRECTIONS = ["supports", "contradicts", "neutral"] as const satisfies readonly Direction[];
export const AGGREGATES = ["last", "mean", "sum", "count", "p50", "p90"] as const;
export const COMPARATORS = [">=", ">", "<=", "<"] as const;
/** `content.FEEDBACK`: the metric of a test card that counts the feedback judged to support the claim. */
export const FEEDBACK = "feedback";

/** Limits the hub keeps (evo_agents.hub.tables_canvas and hub.server.canvas.content). */
export const LIMITS = {
  name: 200,
  description: 10_000,
  text: 2_000,
  cardText: 2_000,
  reason: 1_000,
  summary: 4_000,
  url: 2_000,
  tenant: 100,
  windowDays: 90,
  links: 20,
} as const;

/** `tables_canvas.SLUG`. */
export const SLUG = /^[a-z0-9][a-z0-9-]{0,99}$/;
/** A slug from a product's name: lower case, words joined by hyphens, as the hub takes one. */
export function slugOf(name: string): string {
  return name
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase()
    .replace(/đ/g, "d")
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 100);
}

/** A claim's code as the pages name it (`content.CODE`, written as the hub writes it). */
export const CLAIM_CODE = /^CL-[1-9][0-9]{0,8}$/;

/** A key of the project's registry, as a claim's form offers it for a test card's metric and the claim's links. */
export type SignalKeyOption = { name: string; kind: "number" | "feedback"; unit: string };

export type Viewer = { login: string; role: "reader" | "writer" | "admin" | null };

export function canWrite(viewer: Viewer | null): boolean {
  return viewer?.role === "writer" || viewer?.role === "admin";
}

/** `baselines.OVERDUE_STATUSES`: what a past deadline without real evidence asks a reviewer about. */
const OVERDUE_STATUSES: readonly ClaimStatus[] = ["assumption", "testing"];

/**
 * Whether a claim's test card is past its deadline without real evidence that supports it, as a baseline would ask a
 * reviewer to extend or retire it. `today` is the day as the hub writes days, YYYY-MM-DD.
 */
export function isOverdue(claim: Pick<CanvasClaim, "status" | "test_card" | "real_support_count">, today: string): boolean {
  const deadline = claim.test_card?.deadline;
  return Boolean(deadline) && OVERDUE_STATUSES.includes(claim.status) && (deadline as string) < today && claim.real_support_count === 0;
}

/** Today in UTC as the hub writes days: a test card's deadline passes after it (`baselines.today`). */
export function utcToday(now: Date = new Date()): string {
  return now.toISOString().slice(0, 10);
}

/** How many claims hold each status, from the counts the hub gives a canvas (`statuses`). */
export function statusCounts(statuses: Record<string, unknown>): Record<ClaimStatus, number> {
  return Object.fromEntries(STATUSES.map((status) => [status, typeof statuses[status] === "number" ? (statuses[status] as number) : 0])) as Record<
    ClaimStatus,
    number
  >;
}

/** What `testing` lacks of its test card (`rules.TESTING_NEEDS`); empty when it holds them all. */
export function testingLacks(card: Pick<CanvasTestCardIn, "hypothesis" | "test" | "deadline"> | null): ("hypothesis" | "test" | "deadline")[] {
  return (["hypothesis", "test", "deadline"] as const).filter((field) => !card?.[field]);
}

/** The form of a claim: every field as text, as inputs hold it. */
export type ClaimForm = {
  cell: Cell;
  text: string;
  status: ClaimStatus;
  confidence: Confidence;
  delivery: Delivery;
  hypothesis: string;
  test: string;
  metric: string;
  aggregate: string;
  windowDays: string;
  comparator: string;
  threshold: string;
  minSamples: string;
  tenant: string;
  deadline: string;
  useCases: string;
  signalKeys: string[];
};

export function emptyForm(cell: Cell): ClaimForm {
  return {
    cell,
    text: "",
    status: "assumption",
    confidence: "low",
    delivery: "code",
    hypothesis: "",
    test: "",
    metric: "",
    aggregate: "",
    windowDays: "",
    comparator: "",
    threshold: "",
    minSamples: "",
    tenant: "",
    deadline: "",
    useCases: "",
    signalKeys: [],
  };
}

const text = (value: string | number | null | undefined) => (value === null || value === undefined ? "" : String(value));

export function formOf(content: CanvasContent): ClaimForm {
  const card: Partial<CanvasTestCard> = content.test_card ?? {};
  return {
    cell: content.cell,
    text: content.text,
    status: content.status,
    confidence: content.confidence,
    delivery: content.delivery ?? "code",
    hypothesis: text(card.hypothesis),
    test: text(card.test),
    metric: text(card.metric),
    aggregate: text(card.aggregate),
    windowDays: text(card.window_days),
    comparator: text(card.comparator),
    threshold: text(card.threshold),
    minSamples: text(card.min_samples),
    tenant: text(card.tenant),
    deadline: text(card.deadline),
    useCases: content.use_cases.join(", "),
    signalKeys: [...content.signal_keys],
  };
}

function whole(value: string): number | null {
  const found = value.trim();
  return /^[0-9]+$/.test(found) ? Number(found) : null;
}

function number(value: string): number | null {
  const found = value.trim();
  if (found === "") return null;
  const parsed = Number(found);
  return Number.isFinite(parsed) ? parsed : null;
}

/** The test card a form holds, or null when every field of it is empty. */
export function testCardOf(form: ClaimForm): CanvasTestCardIn | null {
  const card: CanvasTestCardIn = {
    hypothesis: form.hypothesis.trim() || null,
    test: form.test.trim() || null,
    metric: form.metric.trim() || null,
    aggregate: (form.aggregate || null) as CanvasTestCardIn["aggregate"],
    window_days: whole(form.windowDays),
    comparator: (form.comparator || null) as CanvasTestCardIn["comparator"],
    threshold: number(form.threshold),
    min_samples: whole(form.minSamples),
    tenant: form.tenant.trim() || null,
    deadline: form.deadline || null,
  };
  return Object.values(card).some((value) => value !== null) ? card : null;
}

/** Use case codes as typed: separated by commas or new lines, each trimmed, without repeats. */
export function codesOf(value: string): string[] {
  return [...new Set(value.split(/[,\n]/).map((code) => code.trim()).filter(Boolean))];
}

/** Two test cards hold the same values, a missing field and null alike. */
function sameCard(a: CanvasTestCardIn | null | undefined, b: CanvasTestCardIn | null | undefined): boolean {
  const keys = new Set([...Object.keys(a ?? {}), ...Object.keys(b ?? {})]) as Set<keyof CanvasTestCardIn>;
  return [...keys].every((key) => (a?.[key] ?? null) === (b?.[key] ?? null));
}

const sameList = (a: readonly string[], b: readonly string[]) => a.length === b.length && [...a].sort().every((item, index) => item === [...b].sort()[index]);

/**
 * The change of an edit: the fields of `form` that differ from `held` (the claim's content at `rev`), or a new claim
 * when `held` is null; null when nothing differs.
 */
export function changeOf(form: ClaimForm, held: { content: CanvasContent; code: string; rev: number } | null): CanvasChange | null {
  const card = testCardOf(form);
  const useCases = codesOf(form.useCases);
  if (held === null) {
    return {
      cell: form.cell,
      text: form.text.trim(),
      status: form.status,
      confidence: form.confidence,
      delivery: form.delivery,
      test_card: card,
      use_cases: useCases,
      signal_keys: form.signalKeys,
    };
  }
  const { content } = held;
  const change: CanvasChange = { claim: held.code, base_rev: held.rev };
  if (form.cell !== content.cell) change.cell = form.cell;
  if (form.text.trim() !== content.text) change.text = form.text.trim();
  if (form.status !== content.status) change.status = form.status;
  if (form.confidence !== content.confidence) change.confidence = form.confidence;
  if (form.delivery !== (content.delivery ?? "code")) change.delivery = form.delivery;
  if (!sameCard(card, content.test_card)) change.test_card = card;
  if (!sameList(useCases, content.use_cases)) change.use_cases = useCases;
  if (!sameList(form.signalKeys, content.signal_keys)) change.signal_keys = form.signalKeys;
  return Object.keys(change).length > 2 ? change : null;
}

/**
 * The writer's form carried over to a newer rev: each field the writer changed from the rev they started at keeps
 * their value, every other field takes the newer rev's, so what someone else changed meanwhile stays.
 */
export function rebase(form: ClaimForm, from: CanvasContent, to: CanvasContent): ClaimForm {
  const started = formOf(from);
  const newest = formOf(to);
  const merged = { ...newest };
  for (const field of Object.keys(form) as (keyof ClaimForm)[]) {
    const mine = form[field];
    const changed = Array.isArray(mine) ? !sameList(mine, started[field] as string[]) : mine !== started[field];
    if (changed) (merged as Record<keyof ClaimForm, unknown>)[field] = mine;
  }
  return merged;
}

/** A claim a 409 says changed since it was read: the rev asked for, the rev it is at, who changed it and what. */
export type Conflict = {
  claim: string;
  baseRev: number;
  rev: number;
  changedBy: string;
  changes: { field: string; before: unknown; after: unknown }[];
};

/** The claims of a 409 of an edit or an undo (`writes.conflict`); empty for any other failure. */
export function conflictsOf(error: unknown): Conflict[] {
  if (!isApiError(error) || error.status !== 409 || !error.info.detail) return [];
  return error.info.detail.flatMap((item) => {
    if (typeof item !== "object" || item === null) return [];
    const found = item as Record<string, unknown>;
    if (typeof found.claim !== "string" || typeof found.rev !== "number") return [];
    const changes = Array.isArray(found.changes)
      ? found.changes.filter((change): change is { field: string; before: unknown; after: unknown } => typeof change?.field === "string")
      : [];
    return [
      {
        claim: found.claim,
        baseRev: typeof found.base_rev === "number" ? found.base_rev : 0,
        rev: found.rev,
        changedBy: typeof found.changed_by === "string" ? found.changed_by : "",
        changes,
      },
    ];
  });
}

/** A field's value in a diff as text: a list joined, a test card's day as it is, nothing as an empty string. */
export function valueText(value: unknown): string {
  if (value === null || value === undefined || value === "") return "";
  if (Array.isArray(value)) return value.map(valueText).join(", ");
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}
