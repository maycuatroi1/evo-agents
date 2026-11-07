import type { Spec, SpecEdge, SpecNode } from "@/lib/systems";

/**
 * The diff of an agent system that @builder proposes, drawn on the designer's canvas: every node and edge of the
 * proposed spec, and those of the saved spec it drops, each marked by what the proposal does to it. A node keeps the
 * proposed position, a dropped one its saved position.
 */
export type Ghost = "added" | "removed" | "changed" | null;
export type DiffNode = { node: SpecNode; ghost: Ghost };
export type DiffEdge = { edge: SpecEdge; ghost: Ghost };
export type SpecDiff = {
  nodes: DiffNode[];
  edges: DiffEdge[];
  counts: { added: number; removed: number; changed: number; addedEdges: number; removedEdges: number };
};

const edgeKey = (edge: SpecEdge) => `${edge.from}\u0000${edge.to}\u0000${edge.kind}`;

/** Two nodes of one id differ when what they are or name differs; a move alone is no change. */
function changed(saved: SpecNode, proposed: SpecNode): boolean {
  return (
    saved.kind !== proposed.kind ||
    (saved.label ?? "") !== (proposed.label ?? "") ||
    (saved.profile ?? "") !== (proposed.profile ?? "") ||
    (saved.version ?? null) !== (proposed.version ?? null) ||
    (saved.graph ?? "") !== (proposed.graph ?? "")
  );
}

export function specDiff(saved: Spec, proposed: Spec): SpecDiff {
  const before = new Map(saved.nodes.map((node) => [node.id, node]));
  const after = new Set(proposed.nodes.map((node) => node.id));
  const nodes: DiffNode[] = proposed.nodes.map((node) => {
    const held = before.get(node.id);
    return { node, ghost: !held ? "added" : changed(held, node) ? "changed" : null };
  });
  for (const node of saved.nodes) if (!after.has(node.id)) nodes.push({ node, ghost: "removed" });

  const savedEdges = new Map(saved.edges.map((edge) => [edgeKey(edge), edge]));
  const proposedKeys = new Set(proposed.edges.map(edgeKey));
  const edges: DiffEdge[] = proposed.edges.map((edge) => {
    const held = savedEdges.get(edgeKey(edge));
    return { edge, ghost: !held ? "added" : held.description !== edge.description ? "changed" : null };
  });
  for (const edge of saved.edges) if (!proposedKeys.has(edgeKey(edge))) edges.push({ edge, ghost: "removed" });

  const count = <T extends { ghost: Ghost }>(items: T[], ghost: Ghost) => items.filter((item) => item.ghost === ghost).length;
  return {
    nodes,
    edges,
    counts: {
      added: count(nodes, "added"),
      removed: count(nodes, "removed"),
      changed: count(nodes, "changed") + count(edges, "changed"),
      addedEdges: count(edges, "added"),
      removedEdges: count(edges, "removed"),
    },
  };
}
