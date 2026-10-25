import type cytoscape from "cytoscape";

import type { Scope } from "@/lib/kg/explorer";

import type { Theme } from "./graph-theme";

/**
 * How the explorer's canvas draws each scope, from the design tokens of the theme in use. Clusters of the overview are
 * filled circles in their cluster's colour, sized by members; members and nodes take their kind's tone and shape, so
 * colour is never the only signal; a cluster's parts are circles too. At rest edges are faint; the node under the
 * pointer or selected keeps its neighbourhood lit, names its edges' types and dims the rest.
 *
 * Labels keep their size on screen: `scale` (labelScale of explorer-layout.ts, the inverse of the zoom) multiplies
 * every text size, so zooming in makes room between labels rather than larger labels. Which labels show is the
 * canvas's call (declutter): a label that would cover another, or another node, is `crowded`.
 */
export function explorerStylesheet(theme: Theme, scope: Scope["kind"], scale = 1): cytoscape.StylesheetJson {
  const px = (size: number) => size * scale;
  const tones = ([1, 2, 3, 4, 5] as const).map((tone) => ({
    selector: `node[tone = ${tone}]`,
    style: { "background-color": theme.tones[tone] },
  }));
  return [
    {
      selector: "node",
      style: {
        label: "data(label)",
        "font-family": theme.font,
        "font-size": px(11),
        color: theme.foreground,
        "text-valign": "bottom",
        "text-halign": "center",
        "text-margin-y": px(4),
        "text-background-color": theme.surface,
        "text-background-opacity": 0.92,
        "text-background-padding": `${px(2)}px`,
        "text-background-shape": "roundrectangle",
        "border-width": 1.5,
        "border-color": theme.surface,
        "overlay-opacity": 0,
      },
    },
    {
      selector: "node.cluster",
      style: {
        shape: "ellipse",
        width: "data(diameter)",
        height: "data(diameter)",
        "background-color": "data(colour)",
        "background-opacity": 0.92,
        "font-size": px(13),
        "font-weight": 500,
        "text-wrap": "wrap",
        "text-max-width": `${px(130)}px`,
      },
    },
    { selector: "node.cluster.filtered", style: { "background-opacity": 0.18, color: theme.subtle } },
    {
      selector: "node.member",
      style: {
        shape: (node: cytoscape.NodeSingular) => node.data("shape") as cytoscape.Css.NodeShape,
        width: "data(diameter)",
        height: "data(diameter)",
        "font-size": px(11),
      },
    },
    ...tones,
    {
      selector: "node.seed",
      style: {
        width: 34,
        height: 34,
        "border-width": 3,
        "border-color": theme.brand,
        "font-weight": 600,
        "font-size": px(12),
      },
    },
    { selector: "node.expanded", style: { "border-width": 2.5, "border-color": theme.foreground } },
    { selector: "node.hub", style: { "border-width": 2, "border-style": "dashed", "border-color": theme.muted } },
    {
      selector: "node.more",
      style: {
        shape: "round-rectangle",
        width: 30,
        height: 16,
        "background-color": theme.surface,
        "border-width": 1.5,
        "border-style": "dashed",
        "border-color": theme.subtle,
        color: theme.muted,
        "font-size": px(10),
      },
    },
    { selector: "node.kind-hidden", style: { display: "none" } },
    {
      selector: "edge",
      style: {
        width: 1,
        "curve-style": "haystack",
        "line-color": theme.subtle,
        opacity: 0.3,
        "overlay-opacity": 0,
      },
    },
    {
      selector: "edge.link",
      style: { width: "data(width)", "curve-style": "straight", opacity: scope === "overview" ? 0.4 : 0.3 },
    },
    { selector: "edge.link.weak", style: { display: "none" } },
    { selector: "edge.link.weak.reveal", style: { display: "element", "line-style": "dashed" } },
    { selector: "edge.pair", style: { width: "mapData(weight, 1, 8, 0.75, 3)" } },
    {
      selector: "edge.relation, edge.more-edge",
      style: {
        "curve-style": "bezier",
        "target-arrow-shape": "triangle",
        "target-arrow-color": theme.subtle,
        "arrow-scale": 0.7,
        width: 1.2,
        opacity: 0.55,
      },
    },
    { selector: "edge.more-edge", style: { "line-style": "dashed" } },
    // What the reader looks at: the node selected or under the pointer, its edges and its neighbours.
    { selector: ".dim", style: { opacity: 0.12, "text-opacity": 0 } },
    { selector: "node.crowded", style: { "text-opacity": 0, "text-background-opacity": 0 } },
    // A node whose label shows is drawn above the others, the more important higher (declutter of explorer-layout.ts).
    { selector: "node[z]", style: { "z-index": (node: cytoscape.NodeSingular) => Number(node.data("z")) } },
    {
      selector: "edge.hl",
      style: {
        "line-color": theme.brand,
        "target-arrow-color": theme.brand,
        opacity: 0.95,
        "z-index": 9,
      },
    },
    {
      selector: "edge.hl.relation, edge.hl.more-edge",
      style: {
        label: "data(rel)",
        "font-family": theme.font,
        "font-size": px(10),
        color: theme.foreground,
        "text-rotation": "autorotate",
        "text-background-color": theme.surface,
        "text-background-opacity": 1,
        "text-background-padding": `${px(1)}px`,
      },
    },
    {
      selector: "node.chosen",
      style: {
        "underlay-color": theme.ring,
        "underlay-opacity": 0.3,
        "underlay-padding": 6,
        "underlay-shape": "ellipse",
        "font-weight": 600,
      },
    },
  ];
}
