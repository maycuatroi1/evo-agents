import { describe, expect, it } from "vitest";

import {
  centroids,
  CLUSTER_PALETTE,
  clusterCentres,
  clusterColour,
  clusterIndex,
  clusterMatrix,
  filterMembers,
  rgba,
  startPositions,
} from "./map";
import { kgKeys } from "./queries";
import { clusterHref, mapHref, mapOwner } from "./routes";
import type { ClusterSummary, MapNode } from "./types";

const summary = (id: string, size: number): ClusterSummary => ({
  id,
  level: 1,
  parent: null,
  label: `label ${id}`,
  size,
  kinds: { Section: size },
  subclusters: 0,
});

describe("cluster map helpers", () => {
  it("colours clusters from the palette and repeats it", () => {
    expect(clusterColour(0)).toBe(CLUSTER_PALETTE[0]);
    expect(clusterColour(CLUSTER_PALETTE.length)).toBe(CLUSTER_PALETTE[0]);
    expect(clusterColour(-1)).toBe(CLUSTER_PALETTE[CLUSTER_PALETTE.length - 1]);
  });

  it("reads hex colours as rgba, grey for anything else", () => {
    expect(rgba("#ff0000")).toEqual([1, 0, 0, 1]);
    expect(rgba("#0f0", 0.5)).toEqual([0, 1, 0, 0.5]);
    expect(rgba("oops", 0.3)).toEqual([0.5, 0.5, 0.5, 0.3]);
  });

  it("puts a symbol on its file and a section on its document", () => {
    expect(mapOwner({ id: "r:a.py::f", kind: "Symbol", props: { file: "r:a.py" } })).toBe("r:a.py");
    expect(mapOwner({ id: "d:doc:x#s", kind: "Section", props: { item: "d:doc:x" } })).toBe("d:doc:x");
    expect(mapOwner({ id: "requirement:KB-01", kind: "Requirement", props: {} })).toBe("requirement:KB-01");
    expect(mapOwner({ id: "r:b.py::g", kind: "Symbol", props: {} })).toBe("r:b.py::g");
  });

  it("indexes clusters in their order", () => {
    expect([...clusterIndex([summary("c1", 3), summary("c0", 9)])]).toEqual([
      ["c1", 0],
      ["c0", 1],
    ]);
  });

  it("starts each point near its cluster's centre, the same way every time", () => {
    const centres = clusterCentres(3, 1000);
    expect(centres).toHaveLength(6);
    expect(centres.slice(0, 2)).toEqual([500, 500]);
    const ids = ["a", "b", "c"];
    const clusterOf = [0, 2, undefined];
    const first = startPositions(ids, clusterOf, centres, 10);
    expect(Array.from(first)).toEqual(Array.from(startPositions(ids, clusterOf, centres, 10)));
    expect(Math.hypot(first[2]! - centres[4]!, first[3]! - centres[5]!)).toBeLessThanOrEqual(10);
  });

  it("finds the centre of each cluster's points, null for an empty one", () => {
    expect(centroids([0, 0, 10, 20, 5, 5, Number.NaN, 1], [0, 0, 2, 0], 3)).toEqual([[5, 10], null, [5, 5]]);
  });

  it("builds a symmetric matrix of the largest clusters and counts the rest", () => {
    const clusters = [summary("a", 9), summary("b", 5), summary("c", 2)];
    const links = [
      { a: "a", b: "b", weight: 4 },
      { a: "b", b: "c", weight: 7 },
    ];
    const matrix = clusterMatrix(clusters, links, 2);
    expect(matrix.clusters.map((c) => c.id)).toEqual(["a", "b"]);
    expect(matrix.cells).toEqual([
      { row: 0, col: 1, weight: 4 },
      { row: 1, col: 0, weight: 4 },
    ]);
    expect(matrix.max).toBe(4);
    expect(matrix.hidden).toBe(1);
  });

  it("filters members by name, id or kind, ignoring case and accents", () => {
    const members: MapNode[] = [
      { id: "docs:doc:runbook#cài-đặt", kind: "Section", name: "Cài đặt", cluster: "c0", sub: null },
      { id: "app:main.py", kind: "File", name: "main.py", cluster: "c0", sub: null },
    ];
    expect(filterMembers(members, "cai dat").map((m) => m.kind)).toEqual(["Section"]);
    expect(filterMembers(members, "FILE").map((m) => m.id)).toEqual(["app:main.py"]);
    expect(filterMembers(members, "  ")).toHaveLength(2);
  });

  it("links to the map with a focus and to one cluster", () => {
    expect(mapHref("demo", "app:main.py", { q: "main", kind: "" })).toBe("/p/demo/kg?q=main&focus=app%3Amain.py#kg-map");
    expect(clusterHref("demo", "c1.2")).toBe("/p/demo/kg/cluster?id=c1.2");
    expect(kgKeys.cluster("demo", "c1")).toEqual(["kg", "demo", "cluster", "c1"]);
    expect(kgKeys.clusters("demo")).toEqual(["kg", "demo", "clusters"]);
  });
});
