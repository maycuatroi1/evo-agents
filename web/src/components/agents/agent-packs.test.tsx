// @vitest-environment jsdom
import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { Agent } from "@/lib/agents";
import { renderVi } from "@/test/render";

import { AgentPacks } from "./agent-packs";

const AGENT: Agent = {
  handle: "reviewer",
  project: "demo",
  name: "Reviewer",
  description: "",
  instructions: "",
  color: "#2563eb",
  icon_url: null,
  avatar_sha256: null,
  is_default: false,
  permissions: {},
  restricted: [],
  packs: [
    { pack: "acme/notes", plugin: "acme", name: "notes", version: "1.2.0" },
    { pack: "acme/lint", plugin: "acme", name: "lint", version: "2.0.0" },
  ],
  version: 2,
  created_by: "octo",
  created_at: "2026-10-14T03:00:00Z",
  updated_at: "2026-10-14T03:00:00Z",
  deleted_at: null,
};

describe("AgentPacks", () => {
  it("lists each pack the agent attaches with the version it pins, in the agent's order", () => {
    renderVi(<AgentPacks agent={AGENT} />);
    const list = screen.getByRole("list", { name: "Gói agent" });
    const rows = within(list).getAllByRole("listitem");
    expect(rows.map((row) => [row.dataset.value, row.textContent])).toEqual([
      ["acme/notes", "acme/notesv1.2.0"],
      ["acme/lint", "acme/lintv2.0.0"],
    ]);
  });

  it("says when the agent attaches none, and shows no card for the project's default agent", () => {
    const { unmount } = renderVi(<AgentPacks agent={{ ...AGENT, packs: [] }} />);
    expect(screen.getByTestId("agent-packs-none")).toBeInTheDocument();
    unmount();
    renderVi(<AgentPacks agent={{ ...AGENT, handle: "demo", is_default: true, packs: [] }} />);
    expect(screen.queryByTestId("agent-packs")).toBeNull();
  });
});
