// @vitest-environment jsdom
import { QueryClientProvider } from "@tanstack/react-query";
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { createApiClient } from "@/lib/api/client";
import type { EntityEdge, EntityGraphData, EntityGraphNode, WorkingSet } from "@/lib/kg/entity-graph";
import { entityKeys } from "@/lib/kg/entity-queries";
import { makeQueryClient } from "@/lib/query-client";
import { renderVi } from "@/test/render";

import { EntityView } from "./entity-view";

// The empty state reads its setup (GET /v1/kg/{project}/entities/setup): a graph with the step off.
vi.mock("@/lib/api/browser", () => ({
  browserApi: () =>
    createApiClient({
      baseUrl: "http://hub.test",
      fetch: async (request) =>
        request.url.endsWith("/entities/setup")
          ? Response.json({ strategy: null, step_on: false, turns_on: "entity-graph-api", sink: "llm", sink_problem: null, can_change: true, building: null })
          : Response.json({ error: "not_found", message: "not in this test" }, { status: 404 }),
    }),
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn() }),
  usePathname: () => "/p/demo/kg",
  useSearchParams: () => new URLSearchParams(),
}));

// sigma draws with WebGL, which jsdom has not: a list of buttons stands in for the nodes it draws, a click selecting one
// and a double click opening its neighbours, as the real canvas calls back.
vi.mock("./entity-canvas", () => ({
  default: ({ set, selectedNode, onSelectNode, onExpand }: { set: WorkingSet; selectedNode: string | null; onSelectNode: (id: string) => void; onExpand: (id: string) => void }) => (
    <div data-testid="kg-entity-canvas" data-selected={selectedNode ?? ""} data-edges={set.edges.length}>
      {set.nodes.map((node) => (
        <button key={node.id} type="button" data-testid="stub-entity" data-id={node.id} onClick={() => onSelectNode(node.id)} onDoubleClick={() => onExpand(node.id)}>
          {node.name}
        </button>
      ))}
    </div>
  ),
}));

const label = { level: "internal", location: "any", integrity: "U" };
const graph = { build_id: 7, content_hash: "sha256:" + "a".repeat(64), nodes: 40, edges: 90, finished_at: "2026-10-04T10:00:00Z", latest: true };
const node = (id: string, kind: string, name: string, docs: number): EntityGraphNode => ({
  id,
  kind,
  name,
  type: kind.toLowerCase(),
  description: null,
  docs,
  degree: 1,
  status: "inferred",
  conf: 0.9,
  model: "anthropic/claude-haiku-5.5",
  label,
});
const edge = (id: string, src: string, dst: string): EntityEdge => ({
  id,
  src,
  rel: "designed",
  dst,
  keywords: "designed",
  description: null,
  weight: 2,
  conf: 0.8,
  status: "inferred",
  model: "anthropic/claude-haiku-5.5",
  sources: 1,
  label,
});
const ENTITIES: EntityGraphData = {
  graph,
  limit: 150,
  nodes: [node("entity:ada", "Person", "Ada Lovelace", 9), node("entity:engine", "Artifact", "Analytical Engine", 6), node("entity:london", "Location", "London", 2)],
  edges: [edge("r:1", "entity:ada", "entity:engine")],
  total_nodes: 3,
  total_edges: 1,
  truncated: false,
};
const EMPTY: EntityGraphData = { ...ENTITIES, nodes: [], edges: [], total_nodes: 0, total_edges: 0 };

function renderView(data: EntityGraphData, documents: number | null = 1200) {
  const client = makeQueryClient();
  client.setQueryData(entityKeys.graph("demo", 150), data);
  const onChange = vi.fn();
  renderVi(
    <QueryClientProvider client={client}>
      <EntityView project="demo" state={{ focus: null, depth: 1, limit: 150 }} documents={documents} onChange={onChange} />
    </QueryClientProvider>,
  );
  return { onChange };
}

describe("EntityView", () => {
  it("draws the linked entities and lists the isolated ones beside the canvas", async () => {
    renderView(ENTITIES);
    const canvas = await screen.findByTestId("kg-entity-canvas");
    expect(within(canvas).getAllByTestId("stub-entity").map((button) => button.dataset.id)).toEqual(["entity:ada", "entity:engine"]);
    expect(within(screen.getByTestId("kg-entity-isolated")).getByText("London")).toBeTruthy();
    expect(screen.getAllByTestId("kg-entity-type").map((button) => button.dataset.kind)).toEqual(["Artifact", "Location", "Person"]);
  });

  it("hides a kind from the legend: its switch says so and what it linked leaves the canvas", async () => {
    const user = userEvent.setup();
    renderView(ENTITIES);
    await screen.findByTestId("kg-entity-canvas");
    const artifact = screen.getAllByTestId("kg-entity-type").find((button) => button.dataset.kind === "Artifact")!;
    await user.click(artifact);
    expect(artifact.getAttribute("aria-pressed")).toBe("false");
    expect(within(screen.getByTestId("kg-entity-canvas")).queryAllByTestId("stub-entity")).toEqual([]);
    expect(within(screen.getByTestId("kg-entity-isolated")).getByText("Ada Lovelace")).toBeTruthy();
  });

  it("selects an entity from the canvas", async () => {
    const user = userEvent.setup();
    renderView(ENTITIES);
    const canvas = await screen.findByTestId("kg-entity-canvas");
    await user.click(within(canvas).getByText("Ada Lovelace"));
    expect(screen.getByTestId("kg-entity-canvas").dataset.selected).toBe("entity:ada");
  });

  it("explains a graph without entities: the reason and the estimate by documents", async () => {
    renderView(EMPTY, 1000);
    const empty = await screen.findByTestId("kg-entities-empty");
    expect(within(empty).getByTestId("kg-entities-estimate").textContent).toContain("1.000");
    expect(within(empty).getByTestId("kg-entities-reason").textContent).toContain("entities");
    expect(screen.queryByTestId("kg-entity-legend")).toBeNull();
  });
});
