import type { components } from "@/lib/api/schema";

/**
 * Agent systems of a project (docs/hub.md, Agent systems; `evo_agents.hub.system_spec` of the API): nodes on a canvas
 * and typed edges between them, kept as spec `evo-agent-system/1` in revisions. A node is the `entry` (whoever writes
 * to the system), an `agent` (a profile of the project, pinned at a version), a `kg` (a named graph of the project), the
 * `curator` or the `orchestrator` (a profile, pinned like an agent). An edge's kind fixes the kinds of its two ends
 * (EDGE_ENDS). The hub checks a spec again on every write and has the last word: what this module checks in the browser
 * only lets the designer say at once which edges fit and keep a draft it can undo.
 */
type Schemas = components["schemas"];
export type SystemOut = Schemas["SystemOut"];
export type SystemSummary = Schemas["SystemSummary"];
export type SystemWritten = Schemas["SystemWritten"];
export type SystemChecked = Schemas["SystemChecked"];
export type SystemProblem = Schemas["ProblemOut"];
export type SystemPin = Schemas["Pin"];
export type TraceSummary = Schemas["TraceSummary"];
export type TraceOut = Schemas["TraceOut"];
export type TraceSpan = Schemas["SpanOut"];

export const SCHEMA = "evo-agent-system/1";
export const NODE_KINDS = ["entry", "agent", "kg", "curator", "orchestrator"] as const;
export type NodeKind = (typeof NODE_KINDS)[number];
/** The nodes that name a profile of the project and pin one of its versions. */
export const PINNED_KINDS: readonly NodeKind[] = ["agent", "orchestrator"];
/** A system has one entry, and at most one curator and one orchestrator. */
export const SINGLE_KINDS: readonly NodeKind[] = ["entry", "curator", "orchestrator"];
/** kind: the kinds it may start from, the kinds it may end at (`system_spec.EDGE_ENDS`). */
export const EDGE_ENDS = {
  interacts: [["entry", "agent"], ["agent"]],
  uses_kg: [["agent"], ["kg"]],
  optimizes: [["curator"], ["agent", "kg"]],
  orchestrates: [["orchestrator"], ["agent"]],
  escalates: [["agent"], ["orchestrator"]],
} as const satisfies Record<string, readonly [readonly NodeKind[], readonly NodeKind[]]>;
export type EdgeKind = keyof typeof EDGE_ENDS;
export const EDGE_KINDS = Object.keys(EDGE_ENDS) as EdgeKind[];

export const SLUG = /^[a-z0-9][a-z0-9-]{0,62}$/;
export const RESERVED_SLUGS = ["validate"];
export const MAX_NAME_CHARS = 80;
export const MAX_PURPOSE_CHARS = 2000;
export const MAX_LABEL_CHARS = 80;
export const MAX_DESCRIPTION_CHARS = 500;
export const MAX_NODES = 100;

export type Position = { x: number; y: number };
export type SpecNode = {
  id: string;
  kind: NodeKind;
  label?: string;
  position: Position;
  /** The handle of the profile, for an agent or the orchestrator. */
  profile?: string;
  /** The version of the profile it pins; null or missing: the hub pins the latest on the next save. */
  version?: number | null;
  /** The named graph of a kg node, `default` for every source. */
  graph?: string;
};
export type SpecEdge = { from: string; to: string; kind: EdgeKind; description: string };
export type Limits = { trace_usd: number; trace_runs: number; daily_usd: number };
export type Spec = {
  schema: string;
  name: string;
  purpose: string;
  limits?: Limits;
  nodes: SpecNode[];
  edges: SpecEdge[];
};

const isRecord = (value: unknown): value is Record<string, unknown> => typeof value === "object" && value !== null && !Array.isArray(value);

/** The spec of a system as the API answers it (`SystemOut.spec`, a mapping), read defensively. */
export function specOf(raw: unknown): Spec {
  const spec = isRecord(raw) ? raw : {};
  const nodes = Array.isArray(spec.nodes) ? spec.nodes.filter(isRecord) : [];
  const edges = Array.isArray(spec.edges) ? spec.edges.filter(isRecord) : [];
  return {
    schema: SCHEMA,
    name: typeof spec.name === "string" ? spec.name : "",
    purpose: typeof spec.purpose === "string" ? spec.purpose : "",
    ...(isRecord(spec.limits) ? { limits: spec.limits as Limits } : {}),
    nodes: nodes.map((node) => {
      const position = isRecord(node.position) ? node.position : {};
      const out: SpecNode = {
        id: String(node.id ?? ""),
        kind: (NODE_KINDS as readonly string[]).includes(String(node.kind)) ? (node.kind as NodeKind) : "agent",
        position: { x: Number(position.x ?? 0) || 0, y: Number(position.y ?? 0) || 0 },
      };
      if (typeof node.label === "string" && node.label) out.label = node.label;
      if (typeof node.profile === "string") out.profile = node.profile;
      if (typeof node.version === "number") out.version = node.version;
      if (typeof node.graph === "string") out.graph = node.graph;
      return out;
    }),
    edges: edges.map((edge) => ({
      from: String(edge.from ?? ""),
      to: String(edge.to ?? ""),
      kind: (EDGE_KINDS as string[]).includes(String(edge.kind)) ? (edge.kind as EdgeKind) : "interacts",
      description: typeof edge.description === "string" ? edge.description : "",
    })),
  };
}

/** A spec as the API takes it: only the keys of evo-agent-system/1, positions rounded to whole pixels. */
export function specBody(spec: Spec): Record<string, unknown> {
  return {
    schema: SCHEMA,
    name: spec.name.trim(),
    purpose: spec.purpose.trim(),
    ...(spec.limits ? { limits: spec.limits } : {}),
    nodes: spec.nodes.map((node) => ({
      id: node.id,
      kind: node.kind,
      ...(node.label ? { label: node.label } : {}),
      position: { x: Math.round(node.position.x), y: Math.round(node.position.y) },
      ...(PINNED_KINDS.includes(node.kind) ? { profile: node.profile ?? "", ...(node.version ? { version: node.version } : {}) } : {}),
      ...(node.kind === "kg" ? { graph: node.graph ?? "default" } : {}),
    })),
    edges: spec.edges.map((edge) => ({ from: edge.from, to: edge.to, kind: edge.kind, description: edge.description.trim() })),
  };
}

/** The spec of a new system: its entry alone, waiting for the agent it talks to. */
export function newSpec(name: string): Spec {
  return { schema: SCHEMA, name: name.trim(), purpose: "", nodes: [{ id: "entry", kind: "entry", position: { x: 0, y: 0 } }], edges: [] };
}

/**
 * The number of the next system New system creates, named "System {n}" at `system-{n}`: the lowest from 1 whose slug
 * no system of `taken` holds, so a new one never needs a name or an address before it exists.
 */
export function nextSystemNumber(taken: Iterable<string>): number {
  const held = new Set(taken);
  let n = 1;
  while (held.has(`system-${n}`)) n += 1;
  return n;
}

/** Why `slug` cannot name a system, or null. */
export function slugProblem(slug: string): "pattern" | "reserved" | null {
  if (!SLUG.test(slug)) return "pattern";
  if (RESERVED_SLUGS.includes(slug)) return "reserved";
  return null;
}

/** A slug made of a name: lowercase words joined by hyphens, cut to the 63 characters a slug takes. */
export function slugOf(name: string): string {
  return name
    .normalize("NFKD")
    .replace(/[̀-ͯ]/g, "")
    .replace(/đ/gi, "d")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 63)
    .replace(/-+$/, "");
}

/** Whether an edge of `kind` may go from a node of kind `from` to one of kind `to`. */
export function fits(kind: EdgeKind, from: NodeKind, to: NodeKind): boolean {
  const [starts, ends] = EDGE_ENDS[kind];
  return (starts as readonly NodeKind[]).includes(from) && (ends as readonly NodeKind[]).includes(to);
}

/** A way to join two nodes: the edge's kind and its direction, which may be the reverse of the drag. */
export type Join = { kind: EdgeKind; from: string; to: string };

/**
 * The edges that may join node `a` to node `b` of `spec`, in either direction (a drag from a kg to an agent means
 * uses_kg from the agent), leaving out a node joined to itself, an edge drawn already, and a second interacts edge
 * from the entry, which talks to one agent.
 */
export function joins(spec: Spec, a: string, b: string): Join[] {
  if (a === b) return [];
  const kindOf = new Map(spec.nodes.map((node) => [node.id, node.kind]));
  const ka = kindOf.get(a);
  const kb = kindOf.get(b);
  if (!ka || !kb) return [];
  const found: Join[] = [];
  for (const kind of EDGE_KINDS) {
    for (const [from, to, kf, kt] of [
      [a, b, ka, kb],
      [b, a, kb, ka],
    ] as const) {
      if (!fits(kind, kf, kt)) continue;
      if (spec.edges.some((edge) => edge.from === from && edge.to === to && edge.kind === kind)) continue;
      if (kind === "interacts" && kf === "entry" && spec.edges.some((edge) => edge.from === from && edge.kind === "interacts")) continue;
      if (!found.some((join) => join.kind === kind && join.from === from)) found.push({ kind, from, to });
    }
  }
  return found;
}

/** A node id not taken in `spec`: the kind with the first free number, `agent-2` after `agent-1`. */
export function freeId(spec: Spec, base: string): string {
  const stem = base.replace(/[^A-Za-z0-9_.-]+/g, "-").replace(/^[^A-Za-z0-9]+/, "").slice(0, 50) || "node";
  const taken = new Set(spec.nodes.map((node) => node.id));
  if (!taken.has(stem)) return stem;
  for (let n = 2; ; n += 1) if (!taken.has(`${stem}-${n}`)) return `${stem}-${n}`;
}

/** Whether `spec` may take one more node of `kind`: a single kind already there may not. */
export function canAdd(spec: Spec, kind: NodeKind): boolean {
  if (spec.nodes.length >= MAX_NODES) return false;
  return !SINGLE_KINDS.includes(kind) || !spec.nodes.some((node) => node.kind === kind);
}

/** Where a new node goes: right of the right-most node, at the height of the first, so it never lands on another. */
export function placeFor(spec: Spec): Position {
  if (!spec.nodes.length) return { x: 0, y: 0 };
  const right = Math.max(...spec.nodes.map((node) => node.position.x));
  const top = Math.min(...spec.nodes.map((node) => node.position.y));
  return { x: right + 260, y: top };
}

/** `spec` with node `id` and every edge that touches it taken away. */
export function withoutNode(spec: Spec, id: string): Spec {
  return { ...spec, nodes: spec.nodes.filter((node) => node.id !== id), edges: spec.edges.filter((edge) => edge.from !== id && edge.to !== id) };
}

/** `spec` with node `id` changed by `patch`. */
export function withNode(spec: Spec, id: string, patch: Partial<SpecNode>): Spec {
  return { ...spec, nodes: spec.nodes.map((node) => (node.id === id ? { ...node, ...patch } : node)) };
}

/** `spec` with edge `index` changed by `patch`, or taken away when `patch` is null. */
export function withEdge(spec: Spec, index: number, patch: Partial<SpecEdge> | null): Spec {
  if (patch === null) return { ...spec, edges: spec.edges.filter((_, at) => at !== index) };
  return { ...spec, edges: spec.edges.map((edge, at) => (at === index ? { ...edge, ...patch } : edge)) };
}

/** The kg nodes agent node `id` uses (its uses_kg edges), by node id. */
export function usedGraphs(spec: Spec, id: string): string[] {
  return spec.edges.filter((edge) => edge.from === id && edge.kind === "uses_kg").map((edge) => edge.to);
}

/** Whether two specs say the same, positions to the whole pixel. */
export function sameSpec(a: Spec, b: Spec): boolean {
  return JSON.stringify(specBody(a)) === JSON.stringify(specBody(b));
}

/** The problems of a spec by where they show: on a node (by id), on an edge (by index), or on the system whole. */
export type PlacedProblems = { nodes: Map<string, SystemProblem[]>; edges: Map<number, SystemProblem[]>; system: SystemProblem[] };

export function placeProblems(problems: readonly SystemProblem[]): PlacedProblems {
  const placed: PlacedProblems = { nodes: new Map(), edges: new Map(), system: [] };
  for (const problem of problems) {
    if (problem.edge !== null && problem.edge !== undefined) placed.edges.set(problem.edge, [...(placed.edges.get(problem.edge) ?? []), problem]);
    else if (problem.node) placed.nodes.set(problem.node, [...(placed.nodes.get(problem.node) ?? []), problem]);
    else placed.system.push(problem);
  }
  return placed;
}

/** The worst level of some problems: error, warning, or null for none. */
export function worst(problems: readonly SystemProblem[] | undefined): "error" | "warning" | null {
  if (!problems?.length) return null;
  return problems.some((problem) => problem.level === "error") ? "error" : "warning";
}

/** The newest version of each profile, by handle, against which a pinned node is outdated. */
export function outdated(node: SpecNode, latest: ReadonlyMap<string, number>): number | null {
  if (!PINNED_KINDS.includes(node.kind) || !node.profile || !node.version) return null;
  const newest = latest.get(node.profile);
  return newest !== undefined && newest > node.version ? newest : null;
}

/**
 * The draft of the designer with what it can undo: the drafts before it (`past`, the latest last) and those undone
 * (`future`). A step that changes nothing is not kept; at most HISTORY steps are.
 */
export const HISTORY = 100;
export type History = { past: Spec[]; present: Spec; future: Spec[] };

export const historyOf = (spec: Spec): History => ({ past: [], present: spec, future: [] });

export function push(history: History, next: Spec): History {
  if (sameSpec(history.present, next) && JSON.stringify(history.present) === JSON.stringify(next)) return history;
  return { past: [...history.past, history.present].slice(-HISTORY), present: next, future: [] };
}

/**
 * The history with `name` in every draft: a rename saved from the page's title while the draft had changes, so the
 * draft moves onto the new revision and an undo never brings the old name back.
 */
export function renamed(history: History, name: string): History {
  const named = (spec: Spec): Spec => ({ ...spec, name });
  return { past: history.past.map(named), present: named(history.present), future: history.future.map(named) };
}

/** Whether `next` is `before` with another name only. */
export function onlyRenamed(before: Spec, next: Spec): boolean {
  return sameSpec({ ...next, name: before.name }, before);
}

export function undo(history: History): History {
  const previous = history.past.at(-1);
  if (!previous) return history;
  return { past: history.past.slice(0, -1), present: previous, future: [history.present, ...history.future] };
}

export function redo(history: History): History {
  const [next, ...rest] = history.future;
  if (!next) return history;
  return { past: [...history.past, history.present], present: next, future: rest };
}
