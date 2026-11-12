import { type CanvasGraph, canvasGraph, mix, RING, restEdgeColour } from "./canvas-graph";
import type { Theme } from "./canvas-theme";
import type { ExplorerElement } from "./explorer";
import type { Tone } from "./graph";

/**
 * The explorer's elements (lib/kg/explorer.ts, ./explorer-working.ts) as the graph its canvas draws. A cluster is a
 * disc in its cluster's colour sized by members, faded when the kinds left hide all of them; a member or a node is a
 * disc in its kind's tone. The node the view started from carries a ring in the brand colour, a node expanded one in
 * the text colour, a hub a soft one, and a "+N" node is hollow. Links between clusters are as wide as their edges and
 * the weaker ones show only while one of their clusters is lit; a relation is an arrow.
 */
export function explorerGraph(elements: readonly ExplorerElement[], theme: Theme): CanvasGraph {
  const graph = canvasGraph();
  const rest = restEdgeColour(theme);
  for (const element of elements) {
    if (element.group !== "nodes") continue;
    const { data } = element;
    const classes = new Set(element.classes ?? []);
    const tone = theme.tones[Number(data.tone) as Tone] ?? theme.tones[5];
    let color = classes.has("cluster") ? String(data.colour ?? theme.tones[1]) : tone;
    let size = Number(data.diameter ?? 16) / 2;
    let ringColor = theme.surface;
    let ring = 0;
    if (classes.has("filtered")) color = mix(color, theme.surface, 0.8);
    if (classes.has("hub")) [ringColor, ring] = [theme.muted, RING.soft];
    if (classes.has("expanded")) [ringColor, ring] = [theme.foreground, RING.strong];
    if (classes.has("seed")) [size, ringColor, ring] = [17, theme.brand, RING.strong];
    if (classes.has("more")) [size, color, ringColor, ring] = [8, theme.surface, theme.subtle, RING.hollow];
    graph.addNode(String(data.id), {
      label: String(data.label ?? ""),
      kind: String(data.kind ?? ""),
      x: 0,
      y: 0,
      size,
      color,
      ringColor,
      ring,
      weight: Number(data.size ?? data.degree ?? 0),
      anchor: data.anchor === undefined ? undefined : String(data.anchor),
      hidden: classes.has("kind-hidden"),
    });
  }
  for (const element of elements) {
    if (element.group !== "edges") continue;
    const { data } = element;
    const classes = new Set(element.classes ?? []);
    const [source, target] = [String(data.source), String(data.target)];
    if (!graph.hasNode(source) || !graph.hasNode(target)) continue;
    const directed = classes.has("relation") || classes.has("more-edge");
    const weight = Number(data.weight ?? 1);
    const attributes = {
      label: String(data.rel ?? ""),
      size: classes.has("link") ? Number(data.width ?? 1) : classes.has("pair") ? 0.75 + ((Math.min(8, Math.max(1, weight)) - 1) / 7) * 2.25 : 1.2,
      color: classes.has("more-edge") ? mix(theme.subtle, theme.surface, 0.3) : rest,
      type: directed ? ("arrow" as const) : ("line" as const),
      weak: classes.has("weak"),
    };
    const key = String(data.id);
    if (graph.hasEdge(key)) continue;
    if (directed) graph.addDirectedEdgeWithKey(key, source, target, attributes);
    else graph.addUndirectedEdgeWithKey(key, source, target, attributes);
  }
  return graph;
}
