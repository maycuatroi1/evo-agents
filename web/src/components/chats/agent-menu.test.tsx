// @vitest-environment jsdom
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { renderVi } from "@/test/render";

import { AgentActionsContext, AgentMenu, type AgentTarget } from "./agent-menu";
import { AgentSheet } from "./agent-sheet";

const REVIEWER: AgentTarget = { project: "demo", handle: "reviewer", name: "Reviewer", color: "#2563eb", icon_url: null, deleted: false };

function face() {
  return <span data-testid="face">R</span>;
}

describe("an agent's menu in a chat", () => {
  it("draws the face as it was, with no button, where the page has no agent sheet", () => {
    renderVi(<AgentMenu agent={REVIEWER}>{face()}</AgentMenu>);
    expect(screen.getByTestId("face")).toBeInTheDocument();
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("names the agent and opens the sheet on Workers and pools or Accounts", async () => {
    const user = userEvent.setup();
    const open = vi.fn();
    renderVi(
      <AgentActionsContext.Provider value={open}>
        <AgentMenu agent={REVIEWER}>{face()}</AgentMenu>
      </AgentActionsContext.Provider>,
    );
    await user.click(screen.getByRole("button", { name: "Mở menu của Reviewer" }));
    const menu = await screen.findByRole("menu");
    expect(within(menu).getByText("@reviewer")).toBeInTheDocument();
    expect(within(menu).getAllByRole("menuitem").map((item) => item.textContent)).toEqual(["Trang agent", "Worker và pool", "Account"]);
    expect(within(menu).getByRole("menuitem", { name: "Trang agent" })).toHaveAttribute("href", "/p/demo/agents/reviewer");
    await user.click(within(menu).getByRole("menuitem", { name: "Worker và pool" }));
    expect(open).toHaveBeenLastCalledWith(REVIEWER, "workers");

    await user.click(screen.getByRole("button", { name: "Mở menu của Reviewer" }));
    await user.click(within(await screen.findByRole("menu")).getByRole("menuitem", { name: "Account" }));
    expect(open).toHaveBeenLastCalledWith(REVIEWER, "accounts");
  });

  it("offers a deleted agent's page only, since nothing of it changes", async () => {
    const user = userEvent.setup();
    renderVi(
      <AgentActionsContext.Provider value={vi.fn()}>
        <AgentMenu agent={{ ...REVIEWER, deleted: true }}>{face()}</AgentMenu>
      </AgentActionsContext.Provider>,
    );
    await user.click(screen.getByRole("button", { name: "Mở menu của Reviewer" }));
    expect(within(await screen.findByRole("menu")).getAllByRole("menuitem").map((item) => item.textContent)).toEqual(["Trang agent"]);
  });
});

describe("the agent sheet", () => {
  const panels = {
    workers: (agent: AgentTarget) => <p data-testid="workers-panel">{agent.handle}</p>,
    accounts: (agent: AgentTarget) => <p data-testid="accounts-panel">{agent.handle}</p>,
  };

  it("shows the tab the menu picked, drawn by the page, and switches tabs", async () => {
    const user = userEvent.setup();
    const onTab = vi.fn();
    renderVi(<AgentSheet target={{ agent: REVIEWER, tab: "workers" }} panels={panels} onTab={onTab} onOpenChange={vi.fn()} />);
    const sheet = screen.getByTestId("agent-sheet");
    expect(within(sheet).getByRole("heading", { name: "Reviewer" })).toBeInTheDocument();
    expect(within(sheet).getByTestId("agent-sheet-page")).toHaveAttribute("href", "/p/demo/agents/reviewer");
    expect(within(sheet).getByTestId("workers-panel")).toHaveTextContent("reviewer");
    await user.click(within(sheet).getByRole("tab", { name: "Account" }));
    expect(onTab).toHaveBeenCalledWith("accounts");
  });

  it("says the project's default agent owns no accounts instead of drawing them", () => {
    const fallback = { ...REVIEWER, handle: "demo", name: "Demo" };
    const first = renderVi(<AgentSheet target={{ agent: fallback, tab: "accounts" }} panels={panels} onTab={vi.fn()} onOpenChange={vi.fn()} />);
    expect(screen.getByTestId("agent-sheet-default")).toHaveTextContent("không có account riêng");
    expect(screen.queryByTestId("accounts-panel")).toBeNull();
    first.unmount();
    renderVi(<AgentSheet target={{ agent: { ...REVIEWER, is_default: false }, tab: "accounts" }} panels={panels} onTab={vi.fn()} onOpenChange={vi.fn()} />);
    expect(screen.getByTestId("accounts-panel")).toBeInTheDocument();
  });
});
