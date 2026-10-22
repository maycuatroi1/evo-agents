// @vitest-environment jsdom
import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { renderVi } from "@/test/render";

import { StateCell } from "./account-cells";
import type { Account } from "./queries";

const NOW = Date.parse("2026-10-14T04:09:00Z");

const ACCOUNT: Account = {
  name: "oauth-a",
  env_var: "CLAUDE_CODE_OAUTH_TOKEN",
  provider: "claude",
  category: "model",
  auth: "oauth",
  projects: ["demo"],
  workers: [],
  weight: 1,
  priority: 0,
  paused_at: null,
  cooldown_until: null,
  cooldown_reason: null,
  last_leased_at: null,
  expires_at: "2026-10-14T09:00:00Z",
  state: "ready",
  last_usage: null,
  last_usage_at: null,
  runs_24h: 0,
  runs_7d: 0,
  cost_24h_usd: 0,
  cost_7d_usd: 0,
  held_by: [],
  shares: [],
  cost_by_principal: [],
};

describe("the state of an OAuth account", () => {
  it("says it is OAuth with the end of its access token and no renewal reminder", () => {
    renderVi(<StateCell account={ACCOUNT} now={NOW} />);
    expect(screen.getByTestId("account-state").dataset.state).toBe("ready");
    expect(screen.getByTestId("account-oauth").textContent).toMatch(/^OAuth, access token đến /);
    expect(screen.queryByTestId("account-renew-reminder")).toBeNull();
  });

  it("says it needs a new sign-in once its refresh token was refused", () => {
    renderVi(<StateCell account={{ ...ACCOUNT, state: "needs-relogin" }} now={NOW} />);
    expect(screen.getByTestId("account-state").dataset.state).toBe("needs-relogin");
    expect(screen.getByTestId("account-state").textContent).toBe("Cần đăng nhập lại");
  });

  it("shows no OAuth line for an account of a pasted token", () => {
    renderVi(<StateCell account={{ ...ACCOUNT, auth: "token" }} now={NOW} />);
    expect(screen.queryByTestId("account-oauth")).toBeNull();
  });
});
