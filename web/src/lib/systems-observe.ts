import type { Position, Spec, SpecEdge, TraceSpan, TraceSummary } from "./systems";

/**
 * Observe mode of an agent system (web/DESIGN.md, Agent systems; docs/hub.md, reading a trace): what the canvas shows
 * of the work going through the system, worked out from what the hub answers. The numbers of a node and of an edge
 * between agents come from `GET .../metrics` (its runs over the system's last traces); the state of a node, the edges
 * lit now and the queries of a knowledge graph come from the spans of the newest traces (`LIVE_TRACES`), which the
 * trace stream keeps current.
 */

export const NODE_STATES = ["idle", "working", "waiting", "failed"] as const;
export type NodeState = (typeof NODE_STATES)[number];

/** The newest traces whose spans the canvas reads for the states of its nodes and the edges lit. */
export const LIVE_TRACES = 5;
/** How long an edge stays lit after work went through it. */
export const LIT_MS = 10_000;
/** The run states of a running span that is not moving on its own: queued for a worker, or waiting on an answer. */
const WAITING_RUN_STATES = new Set(["queued", "waiting", "parked", "review"]);
const INVOKE = "invoke_agent";
const KG_TOOL = /^kg_/;
const DATA_SOURCE = "gen_ai.data_source.id";
const RESUME = "evo.resume_of_run_id";

export type Numbers = {
  count: number;
  errors: number;
  offDesign: number;
  p50Seconds: number | null;
  usd: number;
  tokens: number;
};

export type Metrics = { nodes: Map<string, Numbers>; edges: Map<string, Numbers> };

/** The key of an edge crossed from node `from` to node `to`, whatever the kind of edge. */
export const edgeKey = (from: string, to: string) => `${from}→${to}`;

const num = (value: unknown): number => (typeof value === "number" && Number.isFinite(value) ? value : 0);
const isRecord = (value: unknown): value is Record<string, unknown> => typeof value === "object" && value !== null && !Array.isArray(value);

function numbersOf(row: Record<string, unknown>): Numbers {
  return {
    count: num(row.count),
    errors: num(row.errors),
    offDesign: num(row.off_design),
    p50Seconds: typeof row.p50_seconds === "number" ? row.p50_seconds : null,
    usd: num(row.usd),
    tokens: num(row.input_tokens) + num(row.output_tokens),
  };
}

/** The metrics as the API answers them (`Metrics`: lists of loose mappings), by node and by edge key. */
export function metricsOf(raw: { nodes?: unknown; edges?: unknown } | null | undefined): Metrics {
  const nodes = new Map<string, Numbers>();
  const edges = new Map<string, Numbers>();
  for (const row of Array.isArray(raw?.nodes) ? raw.nodes.filter(isRecord) : []) {
    if (typeof row.node === "string") nodes.set(row.node, numbersOf(row));
  }
  for (const row of Array.isArray(raw?.edges) ? raw.edges.filter(isRecord) : []) {
    if (typeof row.from_node === "string" && typeof row.to_node === "string") edges.set(edgeKey(row.from_node, row.to_node), numbersOf(row));
  }
  return { nodes, edges };
}

/** Work that went from one node to another: a run another node started, a query of a graph, an ask the hub refused. */
export type Crossing = { from: string; to: string; startedAt: number; endedAt: number | null; running: boolean; offDesign: boolean };

const time = (value: string | null | undefined): number | null => {
  const at = value ? Date.parse(value) : Number.NaN;
  return Number.isNaN(at) ? null : at;
};

/** The kg node of `spec` that graph `graph` is, preferring one `from` has a uses_kg edge to. */
function graphNode(spec: Spec, from: string, graph: string): string | null {
  const nodes = spec.nodes.filter((node) => node.kind === "kg" && (node.graph ?? "default") === graph);
  const used = nodes.find((node) => spec.edges.some((edge) => edge.kind === "uses_kg" && edge.from === from && edge.to === node.id));
  return (used ?? nodes[0])?.id ?? null;
}

/** The crossings the spans of a trace show: a run's span with the node it came from (a resumed turn crosses none), a
 * kg query toward its graph's node, and an ask the hub refused toward the node it was for. */
export function crossings(spec: Spec, spans: readonly TraceSpan[]): Crossing[] {
  const out: Crossing[] = [];
  for (const span of spans) {
    if (!span.node) continue;
    const base = { startedAt: time(span.started_at) ?? 0, endedAt: time(span.ended_at), running: span.status === "running", offDesign: span.off_design };
    if (span.name === INVOKE) {
      if (span.from_node && !(RESUME in (span.attributes ?? {}))) out.push({ from: span.from_node, to: span.node, ...base });
    } else if (span.tool && KG_TOOL.test(span.tool)) {
      const graph = span.attributes?.[DATA_SOURCE];
      const to = graphNode(spec, span.node, typeof graph === "string" ? graph : "default");
      if (to) out.push({ from: span.node, to, ...base });
    } else if (span.status === "refused" && span.to_node) {
      out.push({ from: span.node, to: span.to_node, ...base });
    }
  }
  return out;
}

const litAt = (crossing: Crossing, now: number) => crossing.running || (crossing.endedAt ?? crossing.startedAt) >= now - LIT_MS;

/**
 * The state of each node now, from the spans of the newest traces: working while a run of it moves, waiting while its
 * runs are running but queued or waiting on an answer, failed when its latest run failed, else idle. A kg node works
 * while a query just read it; the entry works while any run of these traces moves.
 */
export function nodeStates(spec: Spec, spans: readonly TraceSpan[], now: number): Map<string, NodeState> {
  const out = new Map<string, NodeState>(spec.nodes.map((node) => [node.id, "idle"]));
  const runs = spans.filter((span) => span.name === INVOKE && span.node);
  const byNode = new Map<string, TraceSpan[]>();
  for (const span of runs) byNode.set(span.node as string, [...(byNode.get(span.node as string) ?? []), span]);
  for (const [node, found] of byNode) {
    const running = found.filter((span) => span.status === "running");
    if (running.some((span) => !WAITING_RUN_STATES.has(span.run_state ?? ""))) out.set(node, "working");
    else if (running.length) out.set(node, "waiting");
    else {
      const latest = found.reduce((a, b) => ((time(b.started_at) ?? 0) >= (time(a.started_at) ?? 0) ? b : a));
      if (latest.status === "error") out.set(node, "failed");
    }
  }
  for (const crossing of crossings(spec, spans)) {
    if (spec.nodes.find((node) => node.id === crossing.to)?.kind === "kg" && litAt(crossing, now)) out.set(crossing.to, "working");
  }
  const entry = spec.nodes.find((node) => node.kind === "entry");
  if (entry && runs.some((span) => span.status === "running")) out.set(entry.id, "working");
  return out;
}

/** How many queries each kg node took in the spans read. */
export function graphQueries(spec: Spec, spans: readonly TraceSpan[]): Map<string, number> {
  const out = new Map<string, number>();
  for (const crossing of crossings(spec, spans)) {
    if (spec.nodes.find((node) => node.id === crossing.to)?.kind === "kg") out.set(crossing.to, (out.get(crossing.to) ?? 0) + 1);
  }
  return out;
}

export type EdgeActivity = { count: number; lit: boolean; offDesign: number };
/** An edge the spec lacks that work crossed: drawn dashed in the warning colour. */
export type ExtraEdge = EdgeActivity & { from: string; to: string };

/**
 * What went through each edge of the spec (by its index) and through edges it lacks: the count (the metrics' for
 * edges between agents, the queries read for a uses_kg edge), whether work crossed it in the last LIT_MS, and how many
 * crossings were off the design. A crossing the spec has an edge for lands on that edge, whatever its kind.
 */
export function edgeActivity(spec: Spec, metrics: Metrics, spans: readonly TraceSpan[], now: number) {
  const found = crossings(spec, spans);
  const byKey = new Map<string, Crossing[]>();
  for (const crossing of found) byKey.set(edgeKey(crossing.from, crossing.to), [...(byKey.get(edgeKey(crossing.from, crossing.to)) ?? []), crossing]);
  const activity = (key: string, kgEdge: boolean): EdgeActivity => {
    const seen = byKey.get(key) ?? [];
    const counted = kgEdge ? undefined : metrics.edges.get(key);
    return {
      count: Math.max(counted?.count ?? 0, seen.length),
      lit: seen.some((crossing) => litAt(crossing, now)),
      offDesign: Math.max(counted?.offDesign ?? 0, seen.filter((crossing) => crossing.offDesign).length),
    };
  };
  const specKeys = new Set(spec.edges.map((edge) => edgeKey(edge.from, edge.to)));
  const edges = new Map<number, EdgeActivity>(spec.edges.map((edge: SpecEdge, index) => [index, activity(edgeKey(edge.from, edge.to), edge.kind === "uses_kg")]));
  const extraKeys = new Map<string, { from: string; to: string }>();
  for (const crossing of found) extraKeys.set(edgeKey(crossing.from, crossing.to), crossing);
  for (const [key, numbers] of metrics.edges) {
    const [from, to] = key.split("→");
    if (numbers.count > 0 && from && to) extraKeys.set(key, { from, to });
  }
  const extra: ExtraEdge[] = [];
  for (const [key, ends] of extraKeys) {
    if (specKeys.has(key)) continue;
    const seen = activity(key, false);
    extra.push({ from: ends.from, to: ends.to, ...seen, offDesign: Math.max(seen.offDesign, seen.count) });
  }
  return { edges, extra };
}

/** Nodes an off-design edge reaches that the spec lacks (an agent named `@handle`), each placed right of where it came from. */
export function ghostNodes(spec: Spec, extra: readonly ExtraEdge[]): { id: string; position: Position }[] {
  const known = new Map(spec.nodes.map((node) => [node.id, node.position]));
  const out: { id: string; position: Position }[] = [];
  for (const edge of extra) {
    for (const [id, other] of [
      [edge.to, edge.from],
      [edge.from, edge.to],
    ] as const) {
      if (known.has(id)) continue;
      const near = known.get(other) ?? { x: 0, y: 0 };
      const position = { x: near.x + 300, y: near.y + 140 * (out.length + 1) };
      known.set(id, position);
      out.push({ id, position });
    }
  }
  return out;
}

/** The traces whose spans the canvas reads: the newest LIVE_TRACES. */
export function liveTraceIds(traces: readonly TraceSummary[]): number[] {
  return traces.slice(0, LIVE_TRACES).map((trace) => trace.id);
}

/** `traces` with `moved` in its place (or first, when new), newest first, as the stream says one moved. */
export function withTrace(traces: readonly TraceSummary[] | undefined, moved: TraceSummary, limit: number): TraceSummary[] {
  const rest = (traces ?? []).filter((trace) => trace.id !== moved.id);
  return [moved, ...rest].sort((a, b) => b.id - a.id).slice(0, limit);
}

/** One bar of a trace's waterfall: where the span starts and how wide it is, in percent of the trace's time. */
export type Bar = { left: number; width: number };

/** The bars of a trace's spans over its time, from the first start to the last end (now for spans still running). */
export function waterfall(spans: readonly TraceSpan[], now: number): Map<number, Bar> {
  const starts = spans.map((span) => time(span.started_at) ?? now);
  const ends = spans.map((span, index) => time(span.ended_at) ?? (span.status === "running" ? now : starts[index]));
  const first = Math.min(...starts);
  const last = Math.max(...ends, first + 1);
  const total = last - first;
  const out = new Map<number, Bar>();
  spans.forEach((span, index) => {
    const left = ((starts[index] - first) / total) * 100;
    const width = Math.max(((ends[index] - starts[index]) / total) * 100, 0.75);
    out.set(span.id, { left: Math.min(left, 99.25), width: Math.min(width, 100 - Math.min(left, 99.25)) });
  });
  return out;
}

/** The spans of a trace in the order of its tree (each under the span that started it), with their depth. */
export function treeOrder(spans: readonly TraceSpan[]): { span: TraceSpan; depth: number }[] {
  const ids = new Set(spans.map((span) => span.id));
  const children = new Map<number | null, TraceSpan[]>();
  for (const span of spans) {
    const parent = span.parent_id !== null && span.parent_id !== undefined && ids.has(span.parent_id) ? span.parent_id : null;
    children.set(parent, [...(children.get(parent) ?? []), span]);
  }
  const out: { span: TraceSpan; depth: number }[] = [];
  const walk = (parent: number | null, depth: number) => {
    for (const span of children.get(parent) ?? []) {
      out.push({ span, depth });
      if (depth < 20) walk(span.id, depth + 1);
    }
  };
  walk(null, 0);
  return out;
}
