import type { components } from "@/lib/api/schema";

import type { Spec, TraceSpan, TraceSummary } from "./systems";
import { crossings, edgeKey } from "./systems-observe";

/**
 * The work list of an agent system's Observe page (web/DESIGN.md, Observe: work and timeline; docs/hub.md, reading a
 * trace): the filters of the list and their counts, the timeline of one work by agent, its hand-offs, and the path it
 * took on the map, worked out from what the hub answers (the traces, a trace's spans and `timeline`).
 */
type Schemas = components["schemas"];
export type TraceTimeline = Schemas["TraceTimeline"];
export type TimelineRun = Schemas["TimelineRun"];
export type TimelineMark = Schemas["TimelineMark"];
export type TimelineSegment = Schemas["TimelineSegment"];
export type WorkSummary = Schemas["WorkSummary"];
export type WorkStatus = NonNullable<TraceSummary["status"]>;

export const WORK_FILTERS = ["all", "needs_you", "running", "failed", "done"] as const;
export type WorkFilter = (typeof WORK_FILTERS)[number];

const INVOKE = "invoke_agent";
const RESUME = "evo.resume_of_run_id";

/** Whether work `trace` waits on a decision put to `login`. */
export const asksYou = (trace: TraceSummary, login: string | null) =>
  login !== null && trace.status === "needs_person" && trace.waiting?.kind === "decision" && trace.waiting.login === login;

/** Whether `trace` shows under `filter` for the member `login`. A trace without a status (older hubs) shows under all. */
export function matches(trace: TraceSummary, filter: WorkFilter, login: string | null): boolean {
  if (filter === "all") return true;
  if (filter === "needs_you") return asksYou(trace, login);
  return trace.status === filter;
}

/** How many of `traces` each filter shows. */
export function filterCounts(traces: readonly TraceSummary[], login: string | null): Record<WorkFilter, number> {
  const out = Object.fromEntries(WORK_FILTERS.map((filter) => [filter, 0])) as Record<WorkFilter, number>;
  for (const trace of traces) for (const filter of WORK_FILTERS) if (matches(trace, filter, login)) out[filter] += 1;
  return out;
}

/** The filter a `?filter=` value names, else all. */
export const filterOf = (value: string | null | undefined): WorkFilter => (WORK_FILTERS as readonly string[]).includes(value ?? "") ? (value as WorkFilter) : "all";

/** The work a `?work=` value names, else none. */
export function workOf(value: string | null | undefined): number | null {
  const id = Number(value);
  return Number.isSafeInteger(id) && id > 0 ? id : null;
}

/** What `usd` is of the budget `limit` of a trace, 0 to 1 and more; null without a limit. */
export const budgetShare = (usd: number, limit: number | null | undefined): number | null => (limit && limit > 0 ? usd / limit : null);

/** The span from the start of `trace` to its last move, or to now while it runs. */
export function workSeconds(trace: Pick<TraceSummary, "started_at" | "last_at" | "status">, now: number): number {
  const start = Date.parse(trace.started_at);
  const end = trace.status === "running" || trace.status === "needs_person" ? Math.max(now, Date.parse(trace.last_at)) : Date.parse(trace.last_at);
  return Number.isNaN(start) || Number.isNaN(end) ? 0 : Math.max(0, (end - start) / 1000);
}

/** A hand-off of a work: a turn an agent handed to another, or an ask the hub refused. */
export type Handoff = {
  id: number;
  from: string;
  to: string;
  at: string;
  status: string;
  refused: boolean;
  offDesign: boolean;
  reason: string | null;
};

/** The hand-offs the spans of a trace show, in the order written: each run started from another node (a resumed turn
 * hands nothing) and each ask, mention or forward the hub refused. */
export function handoffs(spans: readonly TraceSpan[]): Handoff[] {
  const out: Handoff[] = [];
  for (const span of spans) {
    if (!span.node) continue;
    const base = { id: span.id, at: span.started_at, status: span.status, offDesign: span.off_design, reason: span.reason ?? null };
    if (span.name === INVOKE) {
      if (span.from_node && !(RESUME in (span.attributes ?? {}))) out.push({ ...base, from: span.from_node, to: span.node, refused: false });
    } else if (span.status === "refused" && span.to_node) {
      out.push({ ...base, from: span.node, to: span.to_node, refused: true });
    }
  }
  return out;
}

/** The nodes and the edges (by `edgeKey`) the work of `spans` went through. */
export type WorkPath = { nodes: ReadonlySet<string>; edges: ReadonlySet<string> };

export function pathOf(spec: Spec, spans: readonly TraceSpan[]): WorkPath {
  const nodes = new Set<string>();
  const edges = new Set<string>();
  for (const span of spans) if (span.name === INVOKE && span.node) nodes.add(span.node);
  for (const crossing of crossings(spec, spans)) {
    nodes.add(crossing.from);
    nodes.add(crossing.to);
    edges.add(edgeKey(crossing.from, crossing.to));
  }
  const entry = spec.nodes.find((node) => node.kind === "entry");
  const first = spans.find((span) => span.name === INVOKE && span.node && !span.from_node);
  if (entry && first?.node) {
    nodes.add(entry.id);
    edges.add(edgeKey(entry.id, first.node));
  }
  return { nodes, edges };
}

/** The time a timeline covers: from its first segment or mark to its last end, now for what goes on. */
export type Scale = { start: number; end: number };

export function scaleOf(timeline: TraceTimeline, now: number): Scale | null {
  const starts: number[] = [];
  const ends: number[] = [];
  for (const run of timeline.runs ?? []) {
    for (const segment of run.segments) {
      starts.push(Date.parse(segment.started_at));
      ends.push(segment.ended_at ? Date.parse(segment.ended_at) : now);
    }
  }
  for (const mark of timeline.marks ?? []) {
    starts.push(Date.parse(mark.asked_at));
    ends.push(mark.answered_at ? Date.parse(mark.answered_at) : now);
  }
  const valid = (values: number[]) => values.filter((value) => !Number.isNaN(value));
  const first = Math.min(...valid(starts));
  const last = Math.max(...valid(ends));
  if (!Number.isFinite(first) || !Number.isFinite(last)) return null;
  return { start: first, end: Math.max(last, first + 1000) };
}

/** Where `at` falls on `scale`, in percent, clamped. */
export const percentAt = (scale: Scale, at: number) => Math.min(100, Math.max(0, ((at - scale.start) / (scale.end - scale.start)) * 100));

/** The kind a bar of the timeline draws: a run's segment, done or failed once its run ended that way. */
export type BarKind = "queued" | "running" | "parked" | "waiting" | "done" | "failed";

export type TimelineBar = { kind: BarKind; left: number; width: number; open: boolean; startedAt: string; endedAt: string | null };

const FAILED = new Set(["failed", "lost"]);
const ENDED = new Set(["done", "cancelled", ...FAILED]);

/** The bars of one lane: its segments on `scale`, the running segment of an ended run as done or failed. */
export function barsOf(run: TimelineRun, scale: Scale, now: number): TimelineBar[] {
  return run.segments.map((segment) => {
    const start = Date.parse(segment.started_at);
    const end = segment.ended_at ? Date.parse(segment.ended_at) : now;
    const left = percentAt(scale, start);
    const width = Math.max(percentAt(scale, end) - left, 0.6);
    let kind: BarKind = segment.kind;
    if (segment.kind === "running" && ENDED.has(run.state)) kind = FAILED.has(run.state) ? "failed" : "done";
    return { kind, left: Math.min(left, 99.4), width: Math.min(width, 100 - Math.min(left, 99.4)), open: segment.ended_at === null, startedAt: segment.started_at, endedAt: segment.ended_at ?? null };
  });
}

/** The lanes of a timeline: one per agent, its runs in the order of their spans. */
export function lanesOf(timeline: TraceTimeline): { agent: string; runs: TimelineRun[] }[] {
  const out = new Map<string, TimelineRun[]>();
  for (const run of timeline.runs ?? []) out.set(run.agent, [...(out.get(run.agent) ?? []), run]);
  return [...out].map(([agent, runs]) => ({ agent, runs }));
}

/** `count` + 1 evenly spaced times across `scale`, for the axis. */
export function ticksOf(scale: Scale, count = 4): number[] {
  return Array.from({ length: count + 1 }, (_, index) => scale.start + ((scale.end - scale.start) * index) / count);
}

/** The name a person knows an agent by: its profile's display name, else `@handle`. */
export function agentName(handle: string | null | undefined, names: ReadonlyMap<string, string>): string {
  if (!handle) return "";
  const bare = handle.replace(/^@/, "");
  return names.get(bare) ?? `@${bare}`;
}

/** The name of each node of `spec`: its label, else its agent's display name, else `@profile`, else its id. */
export function nodeNames(spec: Spec, names: ReadonlyMap<string, string>): Map<string, string> {
  return new Map(spec.nodes.map((node) => [node.id, node.label || (node.profile ? agentName(node.profile, names) : node.id)]));
}

/** A node's name for a hand-off: the spec's, else an agent outside it by its handle. */
export const nameOfNode = (id: string, nodes: ReadonlyMap<string, string>, names: ReadonlyMap<string, string>) => nodes.get(id) ?? agentName(id, names);

/** A time in hours, minutes and seconds, for "1h 5m", "8m 12s", "42s". */
export function durationParts(seconds: number): { hours: number; minutes: number; seconds: number } {
  const whole = Math.max(0, Math.round(seconds));
  return { hours: Math.floor(whole / 3600), minutes: Math.floor((whole % 3600) / 60), seconds: whole % 60 };
}

/** How near a work is to its budget: past it is danger, from 80 % a warning. */
export const budgetTone = (share: number): "normal" | "warning" | "danger" => (share >= 1 ? "danger" : share >= 0.8 ? "warning" : "normal");
