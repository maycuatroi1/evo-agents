// @vitest-environment jsdom
import { QueryClientProvider } from "@tanstack/react-query";
import { screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { createApiClient } from "@/lib/api/client";
import type { CodeLink, EntityDetail } from "@/lib/kg/entity-graph";
import { makeQueryClient } from "@/lib/query-client";
import { renderVi } from "@/test/render";

import { EntityInspector } from "./entity-inspector";

const label = { level: "internal", location: "any", integrity: "U" };
const graph = { build_id: 7, content_hash: "sha256:" + "a".repeat(64), nodes: 40, edges: 90, finished_at: "2026-10-04T10:00:00Z", latest: true };
const COMMIT = "https://github.com/o/r/blob/abc123/";
const link = (id: string, kind: string, path: string, pkg: string, extra: Partial<CodeLink> = {}): CodeLink => ({
  id,
  kind,
  name: path.split("/").pop() ?? path,
  repo: "r",
  path,
  package: pkg,
  label,
  rels: ["implemented_by"],
  reason: null,
  name_match: null,
  conf: 0.7,
  status: "inferred",
  model: "claude-opus-5-5",
  uri: null,
  line: null,
  ...extra,
});
const CODE = [
  link("dir:r:evo_agents/hub", "Directory", "evo_agents/hub", "evo_agents/hub", { reason: "the relay package", uri: "https://github.com/o/r/tree/abc123/evo_agents/hub" }),
  link("symbol:code:evo_agents/hub/relay.py::Relay", "Symbol", "evo_agents/hub/relay.py", "evo_agents/hub", {
    name: "Relay",
    rels: ["implemented_by", "refers_to"],
    reason: "its class",
    name_match: "Relay",
    conf: 1,
    uri: COMMIT + "evo_agents/hub/relay.py",
    line: 12,
  }),
  link("r:README.md", "File", "README.md", "", { uri: COMMIT + "README.md" }),
];
const ADA = {
  id: "entity:Person:ada",
  kind: "Person",
  name: "Ada Lovelace",
  type: "person",
  description: null,
  docs: 3,
  degree: 0,
  code: CODE.length,
  status: "inferred",
  conf: 0.9,
  model: "anthropic/claude-haiku-5.5",
  label,
};
const DETAIL: EntityDetail = { graph, node: ADA, aliases: [], citations: [], documents: 3, relations: [], neighbours: [], code: CODE, code_links: CODE.length };

// The inspector reads GET /v1/kg/{project}/entity: Ada with her code.
vi.mock("@/lib/api/browser", () => ({
  browserApi: () =>
    createApiClient({
      baseUrl: "http://hub.test",
      fetch: async (request) =>
        new URL(request.url).pathname.endsWith("/entity") ? Response.json(DETAIL) : Response.json({ error: "not_found", message: "not in this test" }, { status: 404 }),
    }),
}));

const actions = { onClose: vi.fn(), onSelectNode: vi.fn(), onSelectEdge: vi.fn(), onExpand: vi.fn(), onFocus: vi.fn(), expanded: () => false };

describe("EntityInspector's code", () => {
  it("lists the entity's code by package, each item linking to its source at the commit and to its node page", async () => {
    renderVi(
      <QueryClientProvider client={makeQueryClient()}>
        <EntityInspector project="demo" id={ADA.id} actions={actions} />
      </QueryClientProvider>,
    );
    const section = await screen.findByTestId("kg-entity-code");
    const packages = within(section).getAllByTestId("kg-entity-code-package");
    expect(packages.map((p) => within(p).getAllByTestId("kg-entity-code-link").length)).toEqual([2, 1]);
    expect(packages[0]?.textContent).toContain("evo_agents/hub");
    const pages = within(section).getAllByTestId("kg-entity-code-node").map((a) => a.getAttribute("href"));
    expect(pages).toContain(`/p/demo/kg/node?id=${encodeURIComponent("symbol:code:evo_agents/hub/relay.py::Relay")}`);
    const sources = within(section).getAllByTestId("kg-entity-code-source").map((a) => a.getAttribute("href"));
    expect(sources).toEqual(["https://github.com/o/r/tree/abc123/evo_agents/hub", COMMIT + "evo_agents/hub/relay.py#L12", COMMIT + "README.md"]);
    expect(section.textContent).toContain("its class");
  });

  it("says so when the entity points to no code", async () => {
    DETAIL.code = [];
    DETAIL.code_links = 0;
    renderVi(
      <QueryClientProvider client={makeQueryClient()}>
        <EntityInspector project="demo" id={ADA.id} actions={actions} />
      </QueryClientProvider>,
    );
    const section = await screen.findByTestId("kg-entity-code");
    expect(within(section).queryAllByTestId("kg-entity-code-link")).toEqual([]);
  });
});
