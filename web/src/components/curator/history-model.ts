import { HISTORY_SOURCES, type HistorySource, HITS_PAGE, type SearchQuery, SESSIONS_PAGE, type SessionsQuery } from "./insight-queries";

/** The URL's search parameters, or anything that reads like them (`useSearchParams`, `URLSearchParams`). */
export type ParamSource = Pick<URLSearchParams, "get" | "getAll">;

/**
 * The History page's pure parts: its search and filters in the URL, the words of a hit with the matched ones marked,
 * what a piece of evidence names, and how far the hub holds the history the machines reported. Every vocabulary here
 * repeats the hub's (evo_agents.hub.history), so a filter in the URL is one the API takes.
 */

/** `history.OUTCOMES`: what the cheap model wrote of a session or a run. */
export const OUTCOMES = ["achieved", "partial", "failed", "abandoned", "unclear"] as const;
export type Outcome = (typeof OUTCOMES)[number];

/** `history.TASK_TYPES`. */
export const TASK_TYPES = ["feature", "fix", "debug", "refactor", "test", "docs", "review", "plan", "ops", "research", "config", "other"] as const;

/** The codes of `history.MAST`, the 14 failure modes of multi-agent systems the model may name. */
export const MAST_MODES = [
  "FM-1.1",
  "FM-1.2",
  "FM-1.3",
  "FM-1.4",
  "FM-1.5",
  "FM-2.1",
  "FM-2.2",
  "FM-2.3",
  "FM-2.4",
  "FM-2.5",
  "FM-2.6",
  "FM-3.1",
  "FM-3.2",
  "FM-3.3",
] as const;

const CLUSTER_KEY = /^[a-z]+\/(?:FM-[1-3]\.[1-6]|none)$/;
const MAX_QUERY = 500;
/** The most pages the URL may name: the API's largest offset over a page. */
const MAX_HITS_PAGE = Math.floor(1000 / HITS_PAGE) + 1;
const MAX_SESSIONS_PAGE = Math.floor(5000 / SESSIONS_PAGE) + 1;

export type HistoryFilters = {
  /** The words searched, as typed: words, "a phrase", -excluded. */
  q: string;
  /** The sources searched; none is every one. */
  sources: HistorySource[];
  /** The page of hits. */
  hits: number;
  kind: "session" | "run" | null;
  outcome: Outcome | null;
  /** A cluster's key, the sessions of that cluster alone. */
  cluster: string | null;
  /** The page of sessions. */
  page: number;
};

export const NO_HISTORY_FILTERS: HistoryFilters = { q: "", sources: [], hits: 1, kind: null, outcome: null, cluster: null, page: 1 };

function pageOf(text: string | null, max: number): number {
  const value = Number(text ?? "1");
  return Number.isInteger(value) && value >= 1 && value <= max ? value : 1;
}

export function readHistoryFilters(source: ParamSource): HistoryFilters {
  const kind = source.get("kind");
  const outcome = source.get("outcome");
  const cluster = source.get("cluster");
  const sources = source.getAll("source").filter((value): value is HistorySource => (HISTORY_SOURCES as readonly string[]).includes(value));
  return {
    q: (source.get("q") ?? "").slice(0, MAX_QUERY),
    sources: HISTORY_SOURCES.filter((value) => sources.includes(value)),
    hits: pageOf(source.get("hits"), MAX_HITS_PAGE),
    kind: kind === "session" || kind === "run" ? kind : null,
    outcome: (OUTCOMES as readonly string[]).includes(outcome ?? "") ? (outcome as Outcome) : null,
    cluster: cluster !== null && CLUSTER_KEY.test(cluster) ? cluster : null,
    page: pageOf(source.get("page"), MAX_SESSIONS_PAGE),
  };
}

/** The URL's query for a set of filters, in a stable order; empty when nothing is set. */
export function historySearch(filters: HistoryFilters): string {
  const params = new URLSearchParams();
  if (filters.q.trim()) params.set("q", filters.q.trim());
  for (const value of filters.sources) params.append("source", value);
  if (filters.hits > 1) params.set("hits", String(filters.hits));
  if (filters.kind) params.set("kind", filters.kind);
  if (filters.outcome) params.set("outcome", filters.outcome);
  if (filters.cluster) params.set("cluster", filters.cluster);
  if (filters.page > 1) params.set("page", String(filters.page));
  const text = params.toString();
  return text ? `?${text}` : "";
}

export function searchQueryOf(filters: HistoryFilters): SearchQuery {
  return { q: filters.q.trim(), sources: filters.sources, offset: (filters.hits - 1) * HITS_PAGE };
}

export function sessionsQueryOf(filters: HistoryFilters): SessionsQuery {
  return { kind: filters.kind, outcome: filters.outcome, cluster: filters.cluster, offset: (filters.page - 1) * SESSIONS_PAGE };
}

/** The words of a hit, the parts the query matched (between « and », as the hub marks them) apart. */
export function markedParts(text: string): { text: string; marked: boolean }[] {
  const parts: { text: string; marked: boolean }[] = [];
  let rest = text;
  while (rest) {
    const open = rest.indexOf("«");
    if (open < 0) break;
    const close = rest.indexOf("»", open + 1);
    if (close < 0) break;
    if (open > 0) parts.push({ text: rest.slice(0, open), marked: false });
    if (close > open + 1) parts.push({ text: rest.slice(open + 1, close), marked: true });
    rest = rest.slice(close + 1);
  }
  if (rest) parts.push({ text: rest, marked: false });
  return parts;
}

/** What a piece of evidence as a finding writes it names: a session, a line of its transcript, an event of a run. */
export type EvidenceRef =
  | { kind: "session"; session: string }
  | { kind: "transcript"; session: string; file: string; line: number }
  | { kind: "run"; run: number; seq: number };

const ID = /^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$/;
const FILE = /^(main|subagents(\/[A-Za-z0-9_][A-Za-z0-9_.-]{0,199}){1,5})$/;

function positive(text: string | undefined): number | null {
  if (!text || !/^[1-9][0-9]{0,15}$/.test(text)) return null;
  const value = Number(text);
  return Number.isSafeInteger(value) ? value : null;
}

export function evidenceRef(evidence: string | null | undefined): EvidenceRef | null {
  const parts = (evidence ?? "").split(":");
  if (parts[0] === "session" && parts.length >= 2 && ID.test(parts[1])) return { kind: "session", session: parts[1] };
  if (parts[0] === "transcript" && (parts.length === 3 || parts.length === 4) && ID.test(parts[1])) {
    const file = parts.length === 4 ? parts[2] : "main";
    const line = positive(parts[parts.length - 1]);
    return line !== null && FILE.test(file) ? { kind: "transcript", session: parts[1], file, line } : null;
  }
  if (parts[0] === "run" && parts.length === 3) {
    const run = positive(parts[1]);
    const seq = positive(parts[2]);
    return run !== null && seq !== null ? { kind: "run", run, seq } : null;
  }
  return null;
}

/** How far the hub holds the history the machines reported, from the figures' `facets` (whole history). */
export type Coverage = {
  /** Sessions whose transcript the hub holds. */
  held: number;
  /** Sessions the machines reported: with a transcript, or with a digest alone. */
  reported: number;
  /** Sessions with a digest alone: what a backfill of their machine would send. */
  waiting: number;
  /** `held` of `reported`, from 0 to 1; null when none was reported. */
  share: number | null;
  /** Sessions and ended runs whose facet is of their content now, of all of them. */
  analyzed: number;
  analyzable: number;
  /** Facets whose last attempt failed; the hub tries them again. */
  failed: number;
};

function count(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) && value > 0 ? Math.round(value) : 0;
}

export function coverageOf(facets: unknown): Coverage | null {
  if (typeof facets !== "object" || facets === null) return null;
  const found = facets as Record<string, unknown>;
  const held = count(found.sessions);
  const waiting = count(found.from_digests);
  const reported = held + waiting;
  return {
    held,
    reported,
    waiting,
    share: reported > 0 ? held / reported : null,
    analyzed: count(found.analyzed) + count(found.runs_analyzed),
    analyzable: held + count(found.runs),
    failed: count(found.failed),
  };
}

/** The numbers of a session's or a run's facet the list shows, 0 for what the facet does not say. */
export function facetNumbers(numbers: Record<string, unknown>): { messages: number; toolErrors: number; corrections: number; cost: number | null } {
  const cost = numbers.cost_usd;
  return {
    messages: count(numbers.messages),
    toolErrors: count(numbers.tool_errors),
    corrections: count(numbers.corrections),
    cost: typeof cost === "number" && Number.isFinite(cost) && cost >= 0 ? cost : null,
  };
}
