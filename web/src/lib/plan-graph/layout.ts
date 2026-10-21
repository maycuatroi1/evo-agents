import dagre from "@dagrejs/dagre";

import type { PlanStep } from "@/lib/plans";

import { type Dependency, dependencyGraph, type StepRelations } from "./model";

export type { Dependency, DependencyKind, StepRelations } from "./model";

/**
 * Where the plan graph puts each step and how each dependency is drawn, top to bottom. Pure: no React, no DOM.
 *
 * dagre ranks and orders the steps; a virtual root above every step without a dependency it can draw holds them all
 * in the first row (left alone, dagre's network simplex sinks such a step next to the one that needs it). dagre may
 * order a row against the plan (4, 2, 1); when over half the pairs in the rows are, the whole drawing is mirrored,
 * which keeps its crossings. Lines are routed here, not by dagre or React Flow: a line runs down through a row only in
 * the lane dagre kept free for it, and turns only at the middle of the gap between two rows, so it never crosses the
 * box of another step.
 */

export type Point = { x: number; y: number };

export type GraphNode = StepRelations & {
  key: string;
  index: number;
  step: PlanStep;
  /** The top-left corner of the step's box. */
  x: number;
  y: number;
  width: number;
  height: number;
  /** 0 for the first row. */
  row: number;
};

/** A dependency with its route: `points` is empty unless `kind` is "drawn". */
export type GraphEdge = Dependency & { points: Point[] };

export type PlanGraph = {
  /** In plan order. */
  nodes: GraphNode[];
  /** Every depends_on entry, drawn or not, by the waiting step's plan order. */
  edges: GraphEdge[];
  width: number;
  height: number;
  rows: number;
  mirrored: boolean;
};

export type GraphSizes = { nodeWidth: number; nodeHeight: number; rowGap: number; columnGap: number };

/** A box of 264 by 52 px, 30 px between rows and 24 between columns: a 9-row plan fits about 700 px at zoom 0.92. */
export const GRAPH_SIZES: GraphSizes = { nodeWidth: 264, nodeHeight: 52, rowGap: 30, columnGap: 24 };

const ROOT = "root";
const id = (position: number) => `s${position}`;

type Placed = { cx: number; cy: number };

/** dagre's placement: each step's centre, and the bend points of each drawn line, in dagre's coordinates. */
function place(steps: readonly PlanStep[], parents: number[][], sizes: GraphSizes) {
  const graph = new dagre.graphlib.Graph();
  graph.setGraph({
    rankdir: "TB",
    nodesep: sizes.columnGap,
    ranksep: sizes.rowGap,
    edgesep: sizes.columnGap,
    marginx: 0,
    marginy: 0,
  });
  graph.setDefaultEdgeLabel(() => ({}));
  graph.setNode(ROOT, { width: 0, height: 0 });
  steps.forEach((_, position) => graph.setNode(id(position), { width: sizes.nodeWidth, height: sizes.nodeHeight }));
  // Heavier than all of a step's other lines together, so no step without a parent is worth sinking below row one.
  const pull = steps.length + 100;
  parents.forEach((above, position) => {
    if (above.length === 0) graph.setEdge(ROOT, id(position), { weight: pull, minlen: 1 });
    for (const parent of above) graph.setEdge(id(parent), id(position), { weight: 1, minlen: 1 });
  });
  dagre.layout(graph);
  const centres: Placed[] = steps.map((_, position) => {
    const node = graph.node(id(position));
    return { cx: node.x ?? 0, cy: node.y ?? 0 };
  });
  const bends: Point[][][] = parents.map((above, position) =>
    above.map((parent) => graph.edge(id(parent), id(position))?.points ?? []),
  );
  return { centres, bends };
}

/** Over half the pairs of steps that share a row stand against plan order. */
function againstPlan(rows: number[], xs: number[]): boolean {
  let pairs = 0;
  let against = 0;
  for (let a = 0; a < rows.length; a += 1) {
    for (let b = a + 1; b < rows.length; b += 1) {
      if (rows[a] !== rows[b]) continue;
      pairs += 1;
      if (xs[a] > xs[b]) against += 1;
    }
  }
  return against * 2 > pairs;
}

/** Removes repeated points and the middle of three on one straight line. */
function simplify(points: Point[]): Point[] {
  const out: Point[] = [];
  for (const point of points) {
    const last = out[out.length - 1];
    if (last && last.x === point.x && last.y === point.y) continue;
    const before = out[out.length - 2];
    if (before && last && ((before.x === last.x && last.x === point.x) || (before.y === last.y && last.y === point.y))) {
      out[out.length - 1] = point;
    } else {
      out.push(point);
    }
  }
  return out;
}

export function layoutPlanGraph(steps: readonly PlanStep[], sizes: GraphSizes = GRAPH_SIZES): PlanGraph {
  const { dependencies, relations, parents } = dependencyGraph(steps);
  if (steps.length === 0) return { nodes: [], edges: [], width: 0, height: 0, rows: 0, mirrored: false };

  const { centres, bends } = place(steps, parents, sizes);
  const { nodeWidth, nodeHeight, rowGap, columnGap } = sizes;

  // Rows: dagre gives every step of a rank one y; a rank with no step (only lines) is closed up.
  const levels = [...new Set(centres.map((c) => Math.round(c.cy)))].sort((a, b) => a - b);
  const rowOf = centres.map((c) => levels.indexOf(Math.round(c.cy)));
  const mirrored = againstPlan(
    rowOf,
    centres.map((c) => c.cx),
  );
  const flip = (x: number) => (mirrored ? -x : x);
  const left = Math.min(...centres.map((c) => flip(c.cx))) - nodeWidth / 2;
  const toX = (x: number) => flip(x) - left;
  const pitch = nodeHeight + rowGap;
  const top = (row: number) => row * pitch;
  const gapBelow = (row: number) => top(row) + nodeHeight + rowGap / 2;

  const nodes: GraphNode[] = steps.map((step, position) => ({
    ...relations[position],
    key: step.key,
    index: step.index,
    step,
    x: toX(centres[position].cx) - nodeWidth / 2,
    y: top(rowOf[position]),
    width: nodeWidth,
    height: nodeHeight,
    row: rowOf[position],
  }));
  const byRow = new Map<number, GraphNode[]>();
  for (const node of nodes) byRow.set(node.row, [...(byRow.get(node.row) ?? []), node]);

  // A lane through `row` at `x`, moved beside the step in the way should dagre's lane ever touch one.
  const lane = (row: number, x: number): number => {
    const boxes = byRow.get(row) ?? [];
    let at = x;
    for (let tries = 0; tries <= boxes.length; tries += 1) {
      const hit = boxes.find((box) => at > box.x - columnGap / 4 && at < box.x + box.width + columnGap / 4);
      if (!hit) return at;
      const centre = hit.x + hit.width / 2;
      at = at < centre ? hit.x - columnGap / 2 : hit.x + hit.width + columnGap / 2;
    }
    return at;
  };

  const drawn = new Map<string, Point[]>();
  parents.forEach((above, position) => {
    const target = nodes[position];
    above.forEach((parent, nth) => {
      const source = nodes[parent];
      const sx = source.x + nodeWidth / 2;
      const tx = target.x + nodeWidth / 2;
      const points: Point[] = [
        { x: sx, y: source.y + nodeHeight },
        { x: sx, y: gapBelow(source.row) },
      ];
      for (let row = source.row + 1; row < target.row; row += 1) {
        // dagre's bend point nearest the middle of this row is the dummy node it placed for the line there.
        const middle = levels[row];
        const bend = bends[position][nth].reduce<Point | null>(
          (best, p) => (best === null || Math.abs(p.y - middle) < Math.abs(best.y - middle) ? p : best),
          null,
        );
        const x = lane(row, bend ? toX(bend.x) : sx);
        points.push({ x, y: gapBelow(row - 1) }, { x, y: gapBelow(row) });
      }
      points.push({ x: tx, y: gapBelow(target.row - 1) }, { x: tx, y: target.y });
      drawn.set(`${parent}>${position}`, simplify(points));
    });
  });

  // A lane may run left of the leftmost step: shift everything so the drawing starts at x 0.
  const lanes = [...drawn.values()].flat();
  const shift = Math.min(0, ...lanes.map((p) => p.x));
  if (shift < 0) {
    for (const node of nodes) node.x -= shift;
    for (const point of lanes) point.x -= shift;
  }

  const positionOf = new Map(steps.map((step, position) => [step.index, position]));
  const edges: GraphEdge[] = dependencies.map((dependency) => {
    if (dependency.kind !== "drawn" || dependency.fromIndex === null) return { ...dependency, points: [] };
    const key = `${positionOf.get(dependency.fromIndex)}>${positionOf.get(dependency.toIndex)}`;
    return { ...dependency, points: drawn.get(key) ?? [] };
  });

  const rows = levels.length;
  return {
    nodes,
    edges,
    width: Math.max(...nodes.map((node) => node.x + node.width), ...lanes.map((p) => p.x)),
    height: rows * nodeHeight + (rows - 1) * rowGap,
    rows,
    mirrored,
  };
}
