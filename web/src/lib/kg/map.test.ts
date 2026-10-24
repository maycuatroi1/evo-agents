import { describe, expect, it } from "vitest";

import { CLUSTER_PALETTE, clusterColour, clusterIndex, filterMembers } from "./map";
import { kgKeys } from "./queries";
import { buildsHref, clusterHref, explorerHref } from "./routes";
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

describe("cluster helpers", () => {
  it("colours clusters from the palette and repeats it", () => {
    expect(clusterColour(0)).toBe(CLUSTER_PALETTE[0]);
    expect(clusterColour(CLUSTER_PALETTE.length)).toBe(CLUSTER_PALETTE[0]);
    expect(clusterColour(-1)).toBe(CLUSTER_PALETTE[CLUSTER_PALETTE.length - 1]);
  });

  it("indexes clusters in their order", () => {
    expect([...clusterIndex([summary("c1", 3), summary("c0", 9)])]).toEqual([
      ["c1", 0],
      ["c0", 1],
    ]);
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

  it("links to the explorer at each scope, to the builds and to one cluster", () => {
    expect(explorerHref("demo", { kind: "overview" })).toBe("/p/demo/kg");
    expect(explorerHref("demo", { kind: "cluster", id: "c:1" })).toBe("/p/demo/kg?cluster=c%3A1");
    expect(explorerHref("demo", { kind: "node", id: "app:main.py" })).toBe("/p/demo/kg?node=app%3Amain.py");
    expect(buildsHref("demo")).toBe("/p/demo/kg/builds");
    expect(buildsHref("demo", true)).toBe("/p/demo/kg/builds?builds=failed#build-history");
    expect(clusterHref("demo", "c1.2")).toBe("/p/demo/kg/cluster?id=c1.2");
    expect(kgKeys.cluster("demo", "c1")).toEqual(["kg", "demo", "cluster", "c1"]);
    expect(kgKeys.clusterSummary("demo")).toEqual(["kg", "demo", "clusters", "summary"]);
  });
});
