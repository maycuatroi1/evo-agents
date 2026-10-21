// @vitest-environment jsdom
import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { renderVi } from "@/test/render";

import { StateCell } from "./account-cells";
import { CostCell } from "./account-columns";
import { ProviderLogo } from "./provider-logo";
import type { Account } from "./queries";

const ACCOUNT: Account = {
  name: "claude-a",
  env_var: "CLAUDE_CODE_OAUTH_TOKEN",
  provider: "claude",
  category: "model",
  auth: "token",
  projects: ["demo"],
  workers: [],
  weight: 1,
  priority: 0,
  paused_at: null,
  cooldown_until: null,
  cooldown_reason: null,
  last_leased_at: null,
  expires_at: null,
  state: "ready",
  last_usage: null,
  last_usage_at: null,
  runs_24h: 1,
  runs_7d: 3,
  cost_24h_usd: 1.5,
  cost_7d_usd: 4,
  held_by: [],
};

describe("the state of an account", () => {
  it("shows no pill for a ready account and keeps the word for a screen reader", () => {
    renderVi(<StateCell account={ACCOUNT} now={0} />);
    const state = screen.getByTestId("account-state");
    expect(state.className).toContain("sr-only");
    expect(state.dataset.state).toBe("ready");
  });

  it("shows a pill once the account is paused", () => {
    renderVi(<StateCell account={{ ...ACCOUNT, state: "paused" }} now={0} />);
    expect(screen.getByTestId("account-state").className).not.toContain("sr-only");
  });
});

describe("the cost of a model account", () => {
  it("calls the figure of a Claude subscription its API value", () => {
    renderVi(<CostCell account={ACCOUNT} />);
    expect(screen.getByTestId("account-cost-label").textContent).toBe("Giá trị API");
  });

  it("calls the figure of an API key account its cost", () => {
    renderVi(<CostCell account={{ ...ACCOUNT, provider: "anthropic", env_var: "ANTHROPIC_API_KEY" }} />);
    expect(screen.getByTestId("account-cost-label").textContent).toBe("Chi phí");
  });

  it("shows no figure when the hub gives none", () => {
    renderVi(<CostCell account={{ ...ACCOUNT, cost_24h_usd: null, cost_7d_usd: null }} />);
    expect(screen.queryByTestId("account-cost")).toBeNull();
  });
});

describe("the logo of a provider", () => {
  it.each(["claude", "anthropic", "gitlab", "notion", "github", "lark", "openai", "ssh", "other"] as const)("is decorative for %s", (provider) => {
    const { container } = renderVi(<ProviderLogo provider={provider} />);
    expect(container.querySelector(`[data-provider="${provider}"]`)?.getAttribute("aria-hidden")).toBe("true");
  });

  it("draws Claude in its brand colour", () => {
    const { container } = renderVi(<ProviderLogo provider="claude" />);
    expect(container.querySelector("svg")?.getAttribute("style")).toContain("rgb(217, 119, 87)");
  });
});
