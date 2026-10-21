import type { GraphEdge, GraphNode, Neighbourhood, NodeDetail } from "@/lib/kg/types";

/**
 * The trace of a coded item in the project's knowledge graph (criterion a4 of plan srs-tasks-plugins): the tasks that
 * implement it or its business rules (Ticket implements UseCase or Requirement), the plan steps that implement those
 * tasks or name the item, the commits and pull requests those steps name, and what verifies it. Read from the item's
 * neighbourhood two steps out, then each plan step's own edges for its commits, which sit a third step away.
 */

export type TraceRef = { id: string; kind: string; name: string | null; status: string | null; uri: string | null };
export type Trace = { tickets: TraceRef[]; steps: TraceRef[]; changes: TraceRef[]; tests: TraceRef[] };

const CHANGE_KINDS = new Set(["Commit", "PullRequest"]);

function ref(node: GraphNode): TraceRef {
  const status = typeof node.props.status === "string" ? node.props.status : null;
  const uri = typeof node.props.uri === "string" && /^https?:\/\//i.test(node.props.uri) ? node.props.uri : null;
  return { id: node.id, kind: node.kind, name: node.name, status, uri };
}

function linked(edges: readonly GraphEdge[], a: string, among: ReadonlySet<string>): boolean {
  return edges.some((edge) => (edge.src === a && among.has(edge.dst)) || (edge.dst === a && among.has(edge.src)));
}

/** What the neighbourhood of `focus` traces to (see the module); the plan steps' commits come from `stepDetails`. */
export function traceOf(hood: Pick<Neighbourhood, "focus" | "nodes" | "edges">, stepDetails: readonly (NodeDetail | undefined)[] = []): Trace {
  const focus = hood.focus;
  const byId = new Map(hood.nodes.map((node) => [node.id, node]));
  const rules = hood.edges.filter((edge) => edge.rel === "child_of" && edge.dst === focus).map((edge) => edge.src);
  const targets = new Set([focus, ...rules]);
  const tickets = hood.nodes.filter((node) => node.kind === "Ticket" && linked(hood.edges, node.id, targets));
  const ticketIds = new Set(tickets.map((node) => node.id));
  const near = new Set([...targets, ...ticketIds]);
  const steps = hood.nodes.filter((node) => node.kind === "PlanStep" && linked(hood.edges, node.id, near));
  const stepIds = new Set(steps.map((node) => node.id));
  const changes = new Map<string, TraceRef>();
  for (const node of hood.nodes) {
    if (CHANGE_KINDS.has(node.kind) && linked(hood.edges, node.id, new Set([...near, ...stepIds]))) changes.set(node.id, ref(node));
  }
  for (const detail of stepDetails) {
    for (const relation of detail?.outgoing ?? []) {
      if (!CHANGE_KINDS.has(relation.kind) || changes.has(relation.node)) continue;
      const known = byId.get(relation.node);
      changes.set(relation.node, known ? ref(known) : { id: relation.node, kind: relation.kind, name: relation.name, status: null, uri: null });
    }
  }
  const tests = hood.edges
    .filter((edge) => edge.rel === "verifies" && targets.has(edge.dst))
    .map((edge) => byId.get(edge.src))
    .filter((node): node is GraphNode => node !== undefined);
  return { tickets: tickets.map(ref), steps: steps.map(ref), changes: [...changes.values()], tests: tests.map(ref) };
}

/** A plan step's node id (`plan:<plan>/step:<id>`) as its plan and step, for its page on the hub; null otherwise. */
export function planStepOf(id: string): { plan: string; step: string } | null {
  const found = /^plan:(.+)\/step:([^/]+)$/.exec(id);
  return found ? { plan: found[1], step: found[2] } : null;
}

/** A commit's node id (`commit:<repo>@<sha>`) as `repo@1a2b3c4`. */
export function shortChange(ref: Pick<TraceRef, "id" | "kind" | "name">): string {
  const commit = /^commit:(.+)@([0-9a-f]{7,40})$/i.exec(ref.id);
  if (commit) return `${commit[1]}@${commit[2].slice(0, 7)}`;
  return ref.name ?? ref.id;
}
