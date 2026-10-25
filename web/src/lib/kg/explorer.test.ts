import { describe, expect, it } from "vitest";

import {
  CLUSTER_DRAWN,
  clusterDiameter,
  clusterElements,
  linkedClusters,
  linkWidth,
  overviewElements,
  parseScope,
  scopeKey,
  shownSize,
  stepOrder,
  strongLinks,
  withScope,
} from "./explorer";
import { collapse, expand, mergeExpansions, moreId, openGroup, parseMoreId, workingElements } from "./explorer-working";
import { nodeLabel } from "./graph";
import type { ClusterDetail, ClusterSummary, GraphNode, MapNode, Neighbourhood } from "./types";

const label = { level: "internal", location: "any", integrity: "U" };
const graph = { build_id: 7, content_hash: "sha256:" + "a".repeat(64), nodes: 5, edges: 4, finished_at: "2026-10-04T10:00:00Z", latest: true };

const cluster = (id: string, size: number, kinds: Record<string, number> = { File: size }): ClusterSummary => ({
  id,
  level: 1,
  parent: null,
  label: `label ${id}`,
  size,
  kinds,
  subclusters: 0,
});

const node = (id: string, kind = "File", degree = 1, hub = false): GraphNode => ({
  id,
  kind,
  name: id.split(":").pop() ?? id,
  status: "parsed",
  conf: 1,
  label,
  props: {},
  aliases: [],
  hop: 1,
  hub,
  degree,
});

const view = (focus: string, nodes: GraphNode[], edges: [string, string, string][], groups: Neighbourhood["groups"] = []): Neighbourhood => ({
  graph,
  focus,
  hops: 1,
  limit: 150,
  group: 6,
  nodes,
  edges: edges.map(([src, rel, dst]) => ({ id: `${src}>${rel}>${dst}`, src, rel, dst, status: "parsed" })),
  groups,
  neighbours: nodes.length - 1,
  left_out: 0,
  truncated: false,
});

describe("explorer labels", () => {
  it("names a path by its last part, with its folder when the file's name says little", () => {
    expect(nodeLabel("web/src/components/srs/findings-model.ts")).toBe("findings-model.ts");
    expect(nodeLabel("web/src/app/(app)/p/[project]/kg/page.tsx")).toBe("kg/page.tsx");
    expect(nodeLabel("evo_agents/hub/__init__.py")).toBe("hub/__init__.py");
    expect(nodeLabel("KB-01")).toBe("KB-01");
    expect(nodeLabel("a/very-long-file-name-that-goes-on-and-on.tsx")).toBe("very-long-file-name-that-go…");
  });
});

describe("explorer scope in the URL", () => {
  it("reads a node before a cluster, the old focus as a node, and the overview without either", () => {
    expect(parseScope(new URLSearchParams("node=app%3Amain.py&cluster=c%3A1"))).toEqual({ kind: "node", id: "app:main.py" });
    expect(parseScope(new URLSearchParams("focus=app%3Amain.py"))).toEqual({ kind: "node", id: "app:main.py" });
    expect(parseScope(new URLSearchParams("cluster=c%3A1"))).toEqual({ kind: "cluster", id: "c:1" });
    expect(parseScope(new URLSearchParams("q=main&cluster=%20"))).toEqual({ kind: "overview" });
    expect(scopeKey({ kind: "cluster", id: "c:1" })).toBe("cluster:c:1");
  });

  it("swaps the scope and keeps the search", () => {
    const params = new URLSearchParams("q=main&kind=File&focus=x&cluster=c%3A1");
    expect(withScope(params, { kind: "node", id: "app:main.py" }).toString()).toBe("q=main&kind=File&node=app%3Amain.py");
    expect(withScope(params, { kind: "overview" }).toString()).toBe("q=main&kind=File");
  });
});

describe("explorer overview", () => {
  const clusters = [cluster("a", 100, { File: 80, PlanStep: 20 }), cluster("b", 25), cluster("c", 4), cluster("d", 1)];
  const links = [
    { a: "a", b: "b", weight: 90 },
    { a: "a", b: "c", weight: 40 },
    { a: "a", b: "d", weight: 2 },
    { a: "b", b: "c", weight: 1 },
  ];

  it("sizes circles by the square root of their members and lines by the log of their edges", () => {
    expect(clusterDiameter(100, 100)).toBe(92);
    expect(clusterDiameter(25, 100)).toBe(57);
    expect(clusterDiameter(0, 100)).toBe(22);
    expect(linkWidth(90, 90)).toBe(7);
    expect(linkWidth(1, 90)).toBe(0.75);
  });

  it("keeps each cluster's heaviest links and marks the rest weak", () => {
    expect([...strongLinks(links, 1)].sort()).toEqual(["a b", "a c", "a d"]);
    const elements = overviewElements(clusters, links);
    const weak = elements.filter((e) => e.classes?.includes("weak")).map((e) => e.data.id);
    expect(weak).toEqual([]);
    expect(elements.filter((e) => e.group === "nodes").map((e) => e.data.colour)).toHaveLength(4);
  });

  it("fades a cluster whose kinds are all hidden, and counts what is left", () => {
    expect(shownSize(clusters[0]!, new Set(["File"]))).toBe(20);
    const elements = overviewElements(clusters, links, new Set(["File"]));
    const faded = elements.filter((e) => e.classes?.includes("filtered")).map((e) => e.data.id);
    expect(faded).toEqual(["b", "c", "d"]);
  });

  it("lists the clusters a cluster links to, heaviest first", () => {
    expect(linkedClusters("c", clusters, links).map(({ cluster: c, weight }) => [c.id, weight])).toEqual([
      ["a", 40],
      ["b", 1],
    ]);
  });
});

describe("explorer cluster", () => {
  const member = (id: string, kind: string, sub: string | null): MapNode => ({ id, kind, name: id, cluster: "c0", sub });
  const detail: ClusterDetail = {
    graph,
    cluster: { ...cluster("c0", 4), subclusters: 2 },
    subclusters: [
      { ...cluster("c0.0", 2, { File: 1, Document: 1 }), level: 2, parent: "c0" },
      { ...cluster("c0.1", 2), level: 2, parent: "c0" },
    ],
    members: [member("m1", "File", "c0.0"), member("m2", "Document", "c0.0"), member("m3", "File", "c0.1"), member("m4", "File", null)],
    edges: [
      [0, 1, 3],
      [0, 2, 1],
    ],
    neighbours: [],
  };

  it("draws a cluster with parts as its parts, joined by the edges between their members, and a loose member alone", () => {
    const drawing = clusterElements(detail, new Set(["File"]));
    expect(drawing).toMatchObject({ mode: "parts", drawn: 2, total: 4 });
    const byId = new Map(drawing.elements.map((e) => [e.data.id, e]));
    expect(byId.get("c0.0")?.classes).toEqual(["cluster"]);
    expect(byId.get("c0.0")?.data).toMatchObject({ size: 2, diameter: 92 });
    expect(byId.get("c0.1")?.classes).toContain("filtered"); // its kinds are all File
    expect(byId.get("m4")?.classes).toContain("member");
    expect(byId.has("m1")).toBe(false);
    const links = drawing.elements.filter((e) => e.group === "edges").map((e) => [e.data.source, e.data.target, e.data.weight]);
    expect(links).toEqual([["c0.0", "c0.1", 1]]); // m1-m2 inside c0.0 is no link; m1-m3 joins the parts
  });

  it("draws a cluster without parts member by member, sized by their edges inside it", () => {
    const drawing = clusterElements({ ...detail, subclusters: [] }, new Set(["Document"]));
    expect(drawing).toMatchObject({ mode: "members", drawn: 4, total: 4 });
    const byId = new Map(drawing.elements.map((e) => [e.data.id, e]));
    expect(byId.get("m1")?.data).toMatchObject({ degree: 4, diameter: 30, tone: 3 });
    expect(byId.get("m1")?.data.parent).toBeUndefined();
    expect(byId.get("m2")?.classes).toContain("kind-hidden");
    expect(drawing.elements.filter((e) => e.group === "edges")).toHaveLength(2);
  });

  it("draws the most linked members of a large cluster and says how many it holds", () => {
    const many: ClusterDetail = {
      ...detail,
      subclusters: [],
      members: Array.from({ length: CLUSTER_DRAWN + 5 }, (_, i) => member(`n${i}`, "File", null)),
      edges: [[CLUSTER_DRAWN + 4, 0, 9]],
    };
    const drawing = clusterElements(many);
    expect(drawing).toMatchObject({ mode: "members", drawn: CLUSTER_DRAWN, total: CLUSTER_DRAWN + 5 });
    const ids = new Set(drawing.elements.map((e) => e.data.id));
    expect(ids.has(`n${CLUSTER_DRAWN + 4}`)).toBe(true);
    expect(drawing.elements.filter((e) => e.group === "edges")).toHaveLength(1);
  });
});

describe("explorer node: the working set", () => {
  const seed = view(
    "s",
    [{ ...node("s"), hop: 0 }, node("a", "Section", 4), node("b", "Commit", 40)],
    [
      ["a", "mentions", "s"],
      ["b", "changes", "s"],
    ],
    [{ key: "in:changes:Commit", rel: "changes", direction: "in", kind: "Commit", total: 9, shown: ["b"] }],
  );
  const fromA = view("a", [{ ...node("a", "Section", 4), hop: 0 }, node("s"), node("d", "Document", 2)], [
    ["a", "mentions", "s"],
    ["a", "part_of", "d"],
  ]);

  it("merges the neighbourhoods in order, each node and edge once, with a +N node for each group cut", () => {
    const set = mergeExpansions("s", [{ id: "s", open: [] }, { id: "a", open: [] }], [seed, fromA]);
    expect([...set.nodes.keys()]).toEqual(["s", "a", "b", "d"]);
    expect(set.nodes.get("d")?.origin).toBe("a");
    expect(set.nodes.get("a")?.expanded).toBe(true);
    expect(set.edges.size).toBe(3);
    expect(set.more).toEqual([{ expansion: "s", group: seed.groups[0], hidden: 8 }]);
    const elements = workingElements(set, new Set(["Commit"]));
    const more = elements.find((e) => e.classes?.includes("more"));
    expect(more?.data).toMatchObject({ id: moreId("s", "in:changes:Commit"), label: "+8 Commit", anchor: "s" });
    expect(more?.classes).toContain("kind-hidden");
    expect(elements.find((e) => e.data.id === "s")?.classes).toContain("seed");
    expect(elements.find((e) => e.data.id === "d")?.data.anchor).toBe("a");
  });

  it("reads a seed asked by an alias as the node its neighbourhood is about", () => {
    const set = mergeExpansions("alias:s", [{ id: "alias:s", open: [] }], [seed]);
    expect(set.seed).toBe("s");
    expect(set.nodes.get("s")?.expanded).toBe(true);
    expect(set.more).toEqual([{ expansion: "s", group: seed.groups[0], hidden: 8 }]);
    expect(workingElements(set).find((e) => e.data.id === "s")?.classes).toContain("seed");
  });

  it("leaves out a hidden node with its edges, and an expansion not read yet", () => {
    const set = mergeExpansions("s", [{ id: "s", open: [] }, { id: "a", open: [] }], [seed, undefined], new Set(["b"]));
    expect([...set.nodes.keys()]).toEqual(["s", "a"]);
    expect([...set.edges.values()].map((e) => e.rel)).toEqual(["mentions"]);
    expect(set.more).toEqual([{ expansion: "s", group: seed.groups[0], hidden: 8 }]);
  });

  it("expands, collapses and opens a group without touching the seed's expansion", () => {
    const start = [{ id: "s", open: [] }];
    const grown = expand(start, "a");
    expect(grown).toEqual([{ id: "s", open: [] }, { id: "a", open: [] }]);
    expect(expand(grown, "a")).toEqual(grown);
    expect(collapse(grown, "a", "s")).toEqual(start);
    expect(collapse(grown, "s", "s")).toEqual(grown);
    expect(openGroup(start, "s", "in:changes:Commit")).toEqual([{ id: "s", open: ["in:changes:Commit"] }]);
    expect(openGroup(start, "b", "out:x:File")).toEqual([{ id: "s", open: [] }, { id: "b", open: ["out:x:File"] }]);
  });

  it("reads back the expansion and group of a +N node, and nothing of another id", () => {
    expect(parseMoreId(moreId("app:a.py", "in:changes:Commit"))).toEqual({ expansion: "app:a.py", group: "in:changes:Commit" });
    expect(parseMoreId("app:a.py")).toBeNull();
  });

  it("steps through the nodes it draws, largest first, past hidden kinds", () => {
    const order = stepOrder([
      { group: "nodes", data: { id: "p", label: "part", size: 9 }, classes: ["cluster"] },
      { group: "nodes", data: { id: "x", label: "x", degree: 2 }, classes: ["member"] },
      { group: "nodes", data: { id: "y", label: "y", degree: 5 }, classes: ["member"] },
      { group: "nodes", data: { id: "z", label: "z", degree: 9 }, classes: ["member", "kind-hidden"] },
      { group: "edges", data: { id: "e", source: "x", target: "y" } },
    ]);
    expect(order).toEqual(["p", "y", "x"]);
  });
});
