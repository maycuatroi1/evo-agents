import { describe, expect, it } from "vitest";

import { buildDurations, durationParts, isWebLink, shortHash } from "./format";
import { displayName, groupRelations, kindStyle, neighboursOf, orderNodes, shortLabel, step } from "./graph";
import { buildsActive, cleanQuery, parseHops } from "./queries";
import { kgHref, nodeHref } from "./routes";
import type { GraphEdge, GraphNode, KgBuild, KgBuilds, Relation } from "./types";

const label = { level: "internal", location: "any", integrity: "U" };

function node(id: string, hop: number, kind = "Section", name: string | null = id): GraphNode {
  return { id, kind, name, status: "parsed", conf: 1, label, props: {}, aliases: [], hop, hub: false };
}

function edge(src: string, rel: string, dst: string): GraphEdge {
  return { id: `${src}-${rel}-${dst}`, src, rel, dst, status: "parsed" };
}

function build(overrides: Partial<KgBuild> = {}): KgBuild {
  return {
    id: 1,
    status: "succeeded",
    job_id: 1,
    requested_by: null,
    config_digest: null,
    runs: 1,
    artifact_sha256: null,
    artifact_size: null,
    artifact_reused_from: null,
    artifact_pruned_at: null,
    content_hash: "sha256:" + "ab".repeat(32),
    nodes: 3,
    edges: 2,
    error: null,
    queued_at: "2026-10-04T10:00:00Z",
    started_at: "2026-10-04T10:00:05Z",
    finished_at: "2026-10-04T10:01:10Z",
    ...overrides,
  };
}

describe("build durations", () => {
  it("measures the wait and the run of a finished build, with or without a clock", () => {
    expect(buildDurations(build(), null)).toEqual({ wait: 5_000, run: 65_000 });
    expect(buildDurations(build(), Date.parse("2030-01-01T00:00:00Z"))).toEqual({ wait: 5_000, run: 65_000 });
  });

  it("counts a queued or running build up to now, and leaves it open without a clock", () => {
    const queued = build({ status: "queued", started_at: null, finished_at: null });
    expect(buildDurations(queued, null)).toEqual({ wait: null, run: null });
    expect(buildDurations(queued, Date.parse("2026-10-04T10:00:42Z"))).toEqual({ wait: 42_000, run: null });
    const running = build({ status: "running", finished_at: null });
    expect(buildDurations(running, Date.parse("2026-10-04T10:00:15Z"))).toEqual({ wait: 5_000, run: 10_000 });
    expect(buildDurations(running, null)).toEqual({ wait: 5_000, run: null });
  });

  it("never goes negative when clocks disagree", () => {
    expect(buildDurations(build({ started_at: "2026-10-04T09:59:00Z" }), null).wait).toBe(0);
  });

  it("splits a duration into the units worth saying", () => {
    expect(durationParts(400)).toEqual({ unit: "underSecond" });
    expect(durationParts(42_900)).toEqual({ unit: "seconds", seconds: 42 });
    expect(durationParts(125_000)).toEqual({ unit: "minutes", minutes: 2, seconds: 5 });
    expect(durationParts(3_780_000)).toEqual({ unit: "hours", hours: 1, minutes: 3 });
  });
});

describe("formatting", () => {
  it("shortens a content hash to its first hex digits", () => {
    expect(shortHash(`sha256:${"0123456789abcdef".repeat(4)}`)).toBe("0123456789ab…");
    expect(shortHash(null)).toBe("-");
    expect(shortHash("abc")).toBe("abc");
  });

  it("links only http and https URIs", () => {
    expect(isWebLink("https://example.test/a")).toBe(true);
    expect(isWebLink("http://example.test/a")).toBe(true);
    expect(isWebLink("javascript:alert(1)")).toBe(false);
    expect(isWebLink("file:///etc/passwd")).toBe(false);
    expect(isWebLink("repo/path.py")).toBe(false);
    expect(isWebLink(null)).toBe(false);
  });

  it("names a node by its name, or its id without one", () => {
    expect(displayName({ id: "a:b", name: "Bê" })).toBe("Bê");
    expect(displayName({ id: "a:b", name: " " })).toBe("a:b");
    expect(displayName({ id: "a:b", name: null })).toBe("a:b");
    expect(shortLabel("x".repeat(40), 10)).toBe(`${"x".repeat(9)}…`);
  });
});

describe("graph model", () => {
  const view = {
    nodes: [node("focus", 0, "Requirement"), node("b", 1, "Section", "Bảo"), node("a", 1, "Section", "An"), node("doc", 2, "Document")],
    edges: [edge("a", "mentions", "focus"), edge("focus", "defines", "b"), edge("b", "mentions", "focus"), edge("a", "part_of", "doc")],
  };

  it("lists the edges of the selected node with the node at the other end, by edge type then direction", () => {
    const rows = neighboursOf(view, "focus");
    expect(rows.map((row) => [row.edge.rel, row.direction, row.node.id])).toEqual([
      ["defines", "out", "b"],
      ["mentions", "in", "a"],
      ["mentions", "in", "b"],
    ]);
    expect(neighboursOf(view, "doc").map((row) => [row.direction, row.node.id])).toEqual([["in", "a"]]);
    expect(neighboursOf(view, "nowhere")).toEqual([]);
  });

  it("orders nodes by step, kind and name, and steps through them both ways", () => {
    const ordered = orderNodes(view.nodes);
    expect(ordered.map((n) => n.id)).toEqual(["focus", "a", "b", "doc"]);
    expect(step(ordered, "focus", 1)).toBe("a");
    expect(step(ordered, "doc", 1)).toBe("focus");
    expect(step(ordered, "focus", -1)).toBe("doc");
    expect(step(ordered, "missing", 1)).toBe("focus");
    expect(step([], "x", 1)).toBe("x");
  });

  it("groups a node's relations by edge type and direction", () => {
    const relation = (rel: string, id: string): Relation => ({ rel, node: id, kind: "Section", name: id, status: "parsed", conf: 1 });
    const groups = groupRelations([relation("in_source", "s")], [relation("part_of", "x"), relation("part_of", "y"), relation("in_source", "z")]);
    expect(groups.map((g) => [g.rel, g.direction, g.relations.length])).toEqual([
      ["in_source", "out", 1],
      ["in_source", "in", 1],
      ["part_of", "in", 2],
    ]);
  });

  it("draws every kind with a colour and a shape, unknown kinds included", () => {
    expect(kindStyle("Requirement")).toEqual({ tone: 2, shape: "diamond" });
    expect(kindStyle("Document").shape).not.toBe(kindStyle("Section").shape);
    expect(kindStyle("SomethingNew")).toEqual({ tone: 5, shape: "ellipse" });
  });
});

describe("queries and routes", () => {
  it("refreshes the status only while something is queued or running", () => {
    const data = (builds: KgBuild[], jobs: KgBuilds["jobs"] = []): KgBuilds => ({ builds, jobs });
    expect(buildsActive(undefined)).toBe(false);
    expect(buildsActive(data([build()]))).toBe(false);
    expect(buildsActive(data([build({ status: "running", finished_at: null })]))).toBe(true);
    expect(buildsActive(data([build()], [{ job_id: 3, status: "todo", build_id: 2 }]))).toBe(true);
  });

  it("reads the search and the steps from the URL", () => {
    expect(cleanQuery("  runbook ")).toBe("runbook");
    expect(cleanQuery(["a", "b"])).toBe("a");
    expect(cleanQuery(undefined)).toBe("");
    expect(cleanQuery("x".repeat(300))).toHaveLength(200);
    expect(parseHops("1")).toBe(1);
    expect(parseHops("2")).toBe(2);
    expect(parseHops("7")).toBe(2);
    expect(parseHops(undefined)).toBe(2);
  });

  it("puts node ids in the query string, encoded", () => {
    expect(nodeHref("demo", "docs:doc:runbook#sổ-tay")).toBe("/p/demo/kg/node?id=docs%3Adoc%3Arunbook%23s%E1%BB%95-tay");
    expect(nodeHref("demo", "a/b", 1)).toBe("/p/demo/kg/node?id=a%2Fb&hops=1");
    expect(kgHref("demo")).toBe("/p/demo/kg");
    expect(kgHref("demo", { q: "KB-01", kind: "Section" })).toBe("/p/demo/kg?q=KB-01&kind=Section");
  });
});
