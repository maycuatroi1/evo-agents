/**
 * What the canvas of an agent system works out about its lines, apart from React Flow so it can be tested: how far
 * each line bows so two lines between the same nodes do not lie on each other, and what the pointer or the selection
 * brings into focus.
 */

/** A line of the canvas, by its React Flow id and its ends. */
export type Line = { id: string; source: string; target: string };

/** What has the pointer or the selection: a node or a line. */
export type Pointer = { node: string } | { line: string } | null;

/** The nodes and lines in focus: the pointed node, its lines and the nodes at their other ends; or a line and its ends. */
export type Focus = { nodes: ReadonlySet<string>; lines: ReadonlySet<string> };

/** The space between two lines that share their ends, in px at their middle. */
export const BEND_STEP = 28;

/** How far each line bows to its own left: 0 for a line alone, and lines between the same two nodes spread apart. */
export function bendsOf(lines: readonly Line[]): Map<string, number> {
  const pairs = new Map<string, Line[]>();
  for (const line of lines) {
    const key = [line.source, line.target].sort().join("\u0000");
    pairs.set(key, [...(pairs.get(key) ?? []), line]);
  }
  const bends = new Map<string, number>();
  for (const group of pairs.values()) {
    const first = [group[0].source, group[0].target].sort()[0];
    group.forEach((line, index) => {
      const offset = (index - (group.length - 1) / 2) * BEND_STEP;
      // A line's left turns over with its direction: measure every offset along the pair's one direction.
      bends.set(line.id, offset === 0 ? 0 : line.source === first ? offset : -offset);
    });
  }
  return bends;
}

/** What `pointer` brings into focus among `lines`, or null when nothing has it. */
export function focusOf(lines: readonly Line[], pointer: Pointer): Focus | null {
  if (!pointer) return null;
  if ("line" in pointer) {
    const line = lines.find((each) => each.id === pointer.line);
    return line ? { nodes: new Set([line.source, line.target]), lines: new Set([line.id]) } : null;
  }
  const touching = lines.filter((line) => line.source === pointer.node || line.target === pointer.node);
  return { nodes: new Set([pointer.node, ...touching.flatMap((line) => [line.source, line.target])]), lines: new Set(touching.map((line) => line.id)) };
}
