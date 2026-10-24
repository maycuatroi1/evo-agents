// @vitest-environment jsdom
import { QueryClientProvider } from "@tanstack/react-query";
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ExplorerElement } from "@/lib/kg/explorer";
import { EXPAND_GROUP } from "@/lib/kg/explorer-working";
import { kgKeys } from "@/lib/kg/queries";
import type { ClusterDetail, ClusterMap, ClusterSummary, GraphNode, GraphSummary, KgBuilds, Neighbourhood, NodeDetail } from "@/lib/kg/types";
import { makeQueryClient } from "@/lib/query-client";
import { renderVi } from "@/test/render";

import { KgExplorer } from "./kg-explorer";

const params = { current: new URLSearchParams() };

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn() }),
  usePathname: () => "/p/demo/kg",
  useSearchParams: () => params.current,
}));

// The canvas is Cytoscape on a <canvas>, which jsdom cannot draw: a list of buttons stands in for its nodes, a click
// selecting one and a double click activating it, as the real canvas calls back.
vi.mock("./explorer-canvas", () => ({
  ExplorerCanvas: ({
    elements,
    selected,
    onSelect,
    onActivate,
  }: {
    elements: ExplorerElement[];
    selected: string | null;
    onSelect: (id: string | null) => void;
    onActivate: (id: string) => void;
  }) => (
    <div data-testid="kg-explorer-canvas" data-selected={selected ?? ""}>
      {elements
        .filter((element) => element.group === "nodes")
        .map((element) => (
          <button
            key={String(element.data.id)}
            type="button"
            data-testid="stub-node"
            data-id={String(element.data.id)}
            data-classes={(element.classes ?? []).join(" ")}
            onClick={() => onSelect(String(element.data.id))}
            onDoubleClick={() => onActivate(String(element.data.id))}
          >
            {String(element.data.label)}
          </button>
        ))}
    </div>
  ),
}));

const label = { level: "internal", location: "any", integrity: "U" };
const graph = { build_id: 7, content_hash: "sha256:" + "a".repeat(64), nodes: 40, edges: 90, finished_at: "2026-10-04T10:00:00Z", latest: true };
const summary = (id: string, name: string, size: number, extra: Partial<ClusterSummary> = {}): ClusterSummary => ({
  id,
  level: 1,
  parent: null,
  label: name,
  size,
  kinds: { File: size - 1, Requirement: 1 },
  subclusters: 0,
  ...extra,
});
const MAP: ClusterMap = {
  graph,
  computed: true,
  reason: null,
  clusters: [summary("c:0", "runbook, cài đặt", 3, { subclusters: 2 }), summary("c:1", "billing", 2)],
  links: [{ a: "c:0", b: "c:1", weight: 3 }],
  nodes: [],
  edges: [],
};
const DETAIL: ClusterDetail = {
  graph,
  cluster: MAP.clusters[0]!,
  subclusters: [summary("c:0.0", "runbook", 2, { level: 2, parent: "c:0" }), summary("c:0.1", "KB-01", 1, { level: 2, parent: "c:0" })],
  members: [
    { id: "docs:doc:runbook", kind: "Document", name: "runbook", cluster: "c:0", sub: "c:0.0" },
    { id: "app:main.py", kind: "File", name: "main.py", cluster: "c:0", sub: "c:0.0" },
    { id: "requirement:KB-01", kind: "Requirement", name: "KB-01", cluster: "c:0", sub: "c:0.1" },
  ],
  edges: [[0, 2, 2]],
  neighbours: [{ id: "c:1", label: "billing", size: 2, weight: 3 }],
};
const SUMMARY: GraphSummary = { graph, kinds: [{ kind: "File", count: 30 }, { kind: "Requirement", count: 10 }], sources: [] };
const BUILDS: KgBuilds = { builds: [], jobs: [] };
const node = (id: string, kind: string, name: string, hop = 1, degree = 2): GraphNode => ({
  id,
  kind,
  name,
  status: "parsed",
  conf: 1,
  label,
  props: {},
  aliases: [],
  hop,
  hub: false,
  degree,
});
const SEED: Neighbourhood = {
  graph,
  focus: "requirement:KB-01",
  hops: 1,
  limit: 150,
  group: EXPAND_GROUP,
  nodes: [node("requirement:KB-01", "Requirement", "KB-01", 0), node("docs:doc:runbook#cài-đặt", "Section", "Cài đặt")],
  edges: [{ id: "e1", src: "docs:doc:runbook#cài-đặt", rel: "mentions", dst: "requirement:KB-01", status: "parsed" }],
  groups: [{ key: "in:mentions:Section", rel: "mentions", direction: "in", kind: "Section", total: 4, shown: ["docs:doc:runbook#cài-đặt"] }],
  neighbours: 4,
  left_out: 0,
  truncated: false,
};
const SECTION: Neighbourhood = {
  ...SEED,
  focus: "docs:doc:runbook#cài-đặt",
  nodes: [node("docs:doc:runbook#cài-đặt", "Section", "Cài đặt", 0), node("requirement:KB-01", "Requirement", "KB-01"), node("docs:doc:runbook", "Document", "runbook")],
  edges: [SEED.edges[0]!, { id: "e2", src: "docs:doc:runbook#cài-đặt", rel: "part_of", dst: "docs:doc:runbook", status: "parsed" }],
  groups: [],
};
const detailOf = (id: string, name: string, kind: string): NodeDetail => ({
  graph,
  node: { id, kind, name, status: "parsed", conf: 1, label, props: { source: "docs" }, aliases: [] },
  evidence: [],
  outgoing: [],
  incoming: id === "requirement:KB-01" ? [{ rel: "mentions", node: "docs:doc:runbook#sổ-tay", kind: "Section", name: "Sổ tay", status: "parsed", conf: 1 }] : [],
});

function withQueries(ui: ReactElement) {
  const client = makeQueryClient();
  client.setQueryData(kgKeys.builds("demo"), BUILDS);
  client.setQueryData(kgKeys.graph("demo"), SUMMARY);
  client.setQueryData(kgKeys.clusterSummary("demo"), MAP);
  client.setQueryData(kgKeys.cluster("demo", "c:0"), DETAIL);
  client.setQueryData(kgKeys.node("demo", "requirement:KB-01"), detailOf("requirement:KB-01", "KB-01", "Requirement"));
  client.setQueryData(kgKeys.node("demo", "docs:doc:runbook#cài-đặt"), detailOf("docs:doc:runbook#cài-đặt", "Cài đặt", "Section"));
  client.setQueryData(kgKeys.neighbourhood("demo", "requirement:KB-01", 1, { group: EXPAND_GROUP, open: [] }), SEED);
  client.setQueryData(kgKeys.neighbourhood("demo", "docs:doc:runbook#cài-đặt", 1, { group: EXPAND_GROUP, open: [] }), SECTION);
  return renderVi(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
}

const errors = { builds: null, graph: null, search: null };
let pushed: string[] = [];

beforeEach(() => {
  params.current = new URLSearchParams();
  pushed = [];
  vi.spyOn(window.history, "pushState").mockImplementation((_state, _unused, url) => {
    pushed.push(String(url));
  });
});

afterEach(() => vi.restoreAllMocks());

describe("knowledge graph explorer", () => {
  it("lands on the overview: the tabs, the figures, the kinds and the clusters, the first tab current", () => {
    withQueries(<KgExplorer project="demo" errors={errors} />);
    expect(screen.getByRole("heading", { level: 1, name: "Đồ thị tri thức" })).toBeInTheDocument();
    expect(screen.getByTestId("kg-tab-explore")).toHaveAttribute("aria-current", "page");
    expect(screen.getByTestId("kg-tab-builds")).toHaveAttribute("href", "/p/demo/kg/builds");
    expect(screen.getByTestId("kg-stat-nodes")).toHaveTextContent("40");
    expect(screen.getByTestId("kg-stat-clusters")).toHaveTextContent("2");
    expect(screen.getAllByTestId("kg-cluster-row").map((row) => row.textContent)).toEqual(["runbook, cài đặt3", "billing2"]);
    expect(screen.getAllByTestId("stub-node").map((n) => n.dataset.id)).toEqual(["c:0", "c:1"]);
    expect(screen.queryByTestId("kg-inspector-cluster")).toBeNull();
    expect(screen.getByTestId("kg-explorer-counts")).toHaveTextContent("2 cụm, 1 liên kết");
  });

  it("selects a cluster from the list into the inspector, and opens it in the URL", async () => {
    withQueries(<KgExplorer project="demo" errors={errors} />);
    await userEvent.click(screen.getAllByTestId("kg-cluster-row")[0]!);
    const inspector = screen.getByTestId("kg-inspector-cluster");
    expect(within(inspector).getByTestId("kg-inspector-title")).toHaveTextContent("runbook, cài đặt");
    expect(within(inspector).getByTestId("kg-inspector-linked")).toHaveTextContent("billing");
    expect(screen.getByTestId("kg-explorer-canvas")).toHaveAttribute("data-selected", "c:0");
    expect(screen.getByTestId("kg-explorer-announcement")).toHaveTextContent("Đã chọn cụm runbook, cài đặt");
    await userEvent.click(within(inspector).getByTestId("kg-inspector-explore"));
    expect(pushed).toEqual(["/p/demo/kg?cluster=c%3A0"]);
    await userEvent.dblClick(screen.getAllByTestId("stub-node")[1]!);
    expect(pushed.at(-1)).toBe("/p/demo/kg?cluster=c%3A1");
  });

  it("hides a kind: its toggle says so and a cluster with nothing else fades", async () => {
    withQueries(<KgExplorer project="demo" errors={errors} />);
    const file = screen.getAllByTestId("kg-kind").find((k) => k.dataset.kind === "File")!;
    await userEvent.click(file);
    expect(file).toHaveAttribute("aria-pressed", "false");
    // each cluster still holds its Requirement, so none fades
    expect(screen.getAllByTestId("stub-node").filter((n) => n.dataset.classes?.includes("filtered"))).toHaveLength(0);
    await userEvent.click(screen.getAllByTestId("kg-kind").find((k) => k.dataset.kind === "Requirement")!);
    expect(screen.getAllByTestId("stub-node").filter((n) => n.dataset.classes?.includes("filtered"))).toHaveLength(2);
    await userEvent.click(screen.getByRole("button", { name: "Hiện tất cả" }));
    expect(file).toHaveAttribute("aria-pressed", "true");
  });

  it("draws a cluster's members in its parts, the cluster in the inspector, and a member explores from there", async () => {
    params.current = new URLSearchParams("cluster=c:0");
    withQueries(<KgExplorer project="demo" errors={errors} />);
    expect(screen.getByTestId("kg-trail")).toHaveTextContent("Tổng quanrunbook, cài đặt");
    const nodes = screen.getAllByTestId("stub-node").map((n) => n.dataset.id);
    expect(nodes).toEqual(["c:0.0", "c:0.1", "docs:doc:runbook", "requirement:KB-01", "app:main.py"]);
    expect(within(screen.getByTestId("kg-inspector-cluster")).getByTestId("kg-inspector-parts")).toHaveTextContent("KB-01");
    expect(within(screen.getByTestId("kg-parts")).getAllByTestId("kg-cluster-row")).toHaveLength(2);
    await userEvent.click(screen.getAllByTestId("stub-node").find((n) => n.dataset.id === "requirement:KB-01")!);
    const inspector = await screen.findByTestId("kg-inspector-node");
    expect(within(inspector).getByTestId("kg-inspector-title")).toHaveTextContent("KB-01");
    await userEvent.click(within(inspector).getByTestId("kg-inspector-explore"));
    expect(pushed.at(-1)).toBe("/p/demo/kg?node=requirement%3AKB-01");
    await userEvent.click(within(screen.getByTestId("kg-trail")).getByRole("button", { name: "Tổng quan" }));
    expect(pushed.at(-1)).toBe("/p/demo/kg");
  });

  it("explores a node: expands a neighbour in place, collapses it, and hides it", async () => {
    params.current = new URLSearchParams("node=requirement:KB-01");
    withQueries(<KgExplorer project="demo" errors={errors} />);
    const ids = () => screen.getAllByTestId("stub-node").map((n) => n.dataset.id);
    expect(ids()).toEqual(["requirement:KB-01", "docs:doc:runbook#cài-đặt", "more\u0000requirement:KB-01\u0000in:mentions:Section"]);
    expect(screen.getByTestId("kg-explorer-canvas")).toHaveAttribute("data-selected", "requirement:KB-01");
    expect(await screen.findByTestId("kg-inspector-relations")).toHaveTextContent("Sổ tay");

    await userEvent.click(screen.getAllByTestId("stub-node")[1]!);
    expect(await screen.findByText("Cài đặt", { selector: "h2" })).toBeInTheDocument();
    await userEvent.click(screen.getByTestId("kg-inspector-expand"));
    expect(ids()).toContain("docs:doc:runbook");
    expect(screen.getAllByTestId("kg-expanded-row")).toHaveLength(2);

    await userEvent.click(screen.getByTestId("kg-inspector-collapse"));
    expect(ids()).not.toContain("docs:doc:runbook");

    await userEvent.click(screen.getByTestId("kg-inspector-hide"));
    expect(ids()).not.toContain("docs:doc:runbook#cài-đặt");
    expect(screen.getByTestId("kg-hidden-nodes")).toHaveTextContent("1 node đang ẩn");
    await userEvent.click(screen.getByRole("button", { name: "Hiện lại" }));
    expect(ids()).toContain("docs:doc:runbook#cài-đặt");
  });
});
