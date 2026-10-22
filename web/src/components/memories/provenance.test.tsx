// @vitest-environment jsdom
import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { renderVi } from "@/test/render";

import { ProvenanceLine } from "./provenance";

const RUN = {
  run: 41,
  agent: "reviewer",
  agent_version: 3,
  origin: "alice",
  hop: 2,
  chat: 9,
  chat_message: 120,
};

describe("ProvenanceLine", () => {
  it("names the run, its agent and version, the person at its origin, its hop and its chat, each id a link", () => {
    renderVi(<ProvenanceLine project="demo" provenance={RUN} />);
    const line = screen.getByTestId("memory-provenance");
    expect(line).toHaveAttribute("data-run", "41");
    expect(line).toHaveAttribute("data-hop", "2");
    expect(screen.getByTestId("provenance-run")).toHaveAttribute("href", "/p/demo/runs/41");
    expect(screen.getByTestId("provenance-run")).toHaveTextContent("Run #41");
    expect(screen.getByTestId("provenance-agent")).toHaveAttribute("href", "/p/demo/agents/reviewer");
    expect(screen.getByTestId("provenance-agent")).toHaveTextContent("@reviewer v3");
    expect(screen.getByTestId("provenance-origin")).toHaveTextContent("cho alice");
    expect(screen.getByTestId("provenance-hop")).toHaveTextContent("hop 2");
    expect(screen.getByTestId("provenance-chat").getAttribute("href")).toContain("9");
  });

  it("names the Curator's proposal of a promoted revision, and nothing for a revision a person wrote", () => {
    const { unmount } = renderVi(
      <ProvenanceLine project="demo" provenance={{ run: null, proposal: 12, agent: null, agent_version: null, origin: null, hop: null, chat: null, chat_message: null }} />,
    );
    expect(screen.getByTestId("provenance-proposal")).toHaveAttribute("href", "/p/demo/curator/proposals/12");
    expect(screen.queryByTestId("provenance-run")).toBeNull();
    unmount();
    renderVi(<ProvenanceLine project="demo" provenance={{ run: null, agent: null, agent_version: null, origin: null, hop: null, chat: null, chat_message: null }} />);
    expect(screen.queryByTestId("memory-provenance")).toBeNull();
  });
});
