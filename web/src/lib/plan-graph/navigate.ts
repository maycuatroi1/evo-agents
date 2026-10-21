/**
 * Moving between the steps of a plan's graph by keyboard. Pure: no React, no DOM.
 *
 * Up and Down go to the step of the row above or below whose centre is nearest across; Left and Right go to the next
 * step along the same row. At an edge the focus stays where it is.
 */

export type GraphDirection = "up" | "down" | "left" | "right";

/** What the move needs of a placed step (a `GraphNode` of layout.ts). */
export type NavigableNode = { key: string; row: number; x: number; width: number };

const centre = (node: NavigableNode) => node.x + node.width / 2;

/** The key of the step a move from `from` lands on, or null when there is none that way (or `from` is not a step). */
export function neighbour(nodes: readonly NavigableNode[], from: string, direction: GraphDirection): string | null {
  const at = nodes.find((node) => node.key === from);
  if (!at) return null;
  if (direction === "left" || direction === "right") {
    const row = nodes.filter((node) => node.row === at.row).sort((a, b) => a.x - b.x);
    const next = row.indexOf(at) + (direction === "right" ? 1 : -1);
    return row[next]?.key ?? null;
  }
  const target = at.row + (direction === "down" ? 1 : -1);
  let best: NavigableNode | null = null;
  for (const node of nodes) {
    if (node.row !== target) continue;
    // The nearest centre; on a tie the one further left, so the move does not depend on the order of `nodes`.
    const distance = Math.abs(centre(node) - centre(at));
    const bestDistance = best ? Math.abs(centre(best) - centre(at)) : Infinity;
    if (distance < bestDistance || (distance === bestDistance && best && node.x < best.x)) best = node;
  }
  return best?.key ?? null;
}

/** The arrow key of a keyboard event as a direction, or null for any other key. */
export function arrowDirection(key: string): GraphDirection | null {
  switch (key) {
    case "ArrowUp":
      return "up";
    case "ArrowDown":
      return "down";
    case "ArrowLeft":
      return "left";
    case "ArrowRight":
      return "right";
    default:
      return null;
  }
}
