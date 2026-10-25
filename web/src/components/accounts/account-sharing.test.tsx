// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { CredentialShare } from "@/components/sharing/model";
import { SharesSection } from "@/components/sharing/shares-section";
import { renderVi } from "@/test/render";

import { costRows, overCeiling, shareTargets } from "./account-sharing";
import type { Account } from "./queries";

const NOW = Date.parse("2026-10-22T04:00:00Z");

const ACCOUNT: Account = {
  name: "claude-main",
  env_var: "CLAUDE_CODE_OAUTH_TOKEN",
  provider: "claude",
  category: "model",
  auth: "token",
  projects: ["demo"],
  workers: [],
  pools: [],
  weight: 1,
  priority: 0,
  paused_at: null,
  cooldown_until: null,
  cooldown_reason: null,
  last_leased_at: null,
  expires_at: null,
  state: "ready",
  last_usage: { status: "allowed", windows: { five_hour: { utilization: 0.85, resets_at: "2026-10-22T06:00:00Z" } } },
  last_usage_at: "2026-10-22T03:00:00Z",
  runs_24h: 0,
  runs_7d: 0,
  cost_24h_usd: 0,
  cost_7d_usd: 0,
  held_by: [],
  shares: [],
  cost_by_principal: [
    { principal: "minh-le", runs_24h: 1, runs_7d: 2, runs_month: 3, cost_24h_usd: 1, cost_7d_usd: 2, cost_month_usd: 3 },
    { principal: "lan-tran", runs_24h: 0, runs_7d: 4, runs_month: 9, cost_24h_usd: 0, cost_7d_usd: 4.5, cost_month_usd: 12.25 },
  ],
};

const SHARE: CredentialShare = {
  id: 5,
  secret: "claude-main",
  credential: "subscription",
  grantee_kind: "unit",
  grantee: "team-a",
  projects: ["demo"],
  level: "A2",
  budget_usd: 50,
  usage_ceiling_pct: 80,
  max_running: 2,
  expires_at: "2026-10-25T00:00:00Z",
  terms_ack_at: "2026-10-20T00:00:00Z",
  created_by: "minh-le",
  created_at: "2026-10-20T00:00:00Z",
  revoked_at: null,
  revoked_by: null,
};

describe("the shares of accounts", () => {
  it("says an account is over a share's ceiling from its windows not reset", () => {
    expect(overCeiling(ACCOUNT, SHARE, NOW)).toBe(true);
    expect(overCeiling(ACCOUNT, { ...SHARE, usage_ceiling_pct: 90 }, NOW)).toBe(false);
    expect(overCeiling(ACCOUNT, SHARE, Date.parse("2026-10-22T07:00:00Z"))).toBe(false);
    expect(overCeiling(undefined, SHARE, NOW)).toBe(false);
  });

  it("offers accounts bound to a project, a Claude one as a subscription", () => {
    expect(shareTargets([ACCOUNT, { ...ACCOUNT, name: "loose", projects: [] }])).toEqual([
      { name: "claude-main", projects: ["demo"], subscription: true },
    ]);
  });

  it("leaves the visitor's own row out of the cost by member", () => {
    expect(costRows([ACCOUNT], "minh-le").map((row) => [row.principal, row.cost_month_usd])).toEqual([["lan-tran", 12.25]]);
  });

  it("lists a share with whom it is for, its limits, and only the pills that say something is wrong", () => {
    const client = new QueryClient();
    renderVi(
      <QueryClientProvider client={client}>
        <SharesSection
          targets={shareTargets([ACCOUNT])}
          shares={[SHARE]}
          onChanged={() => Promise.resolve()}
          extra={() => <span data-testid="extra">!</span>}
          now={NOW}
          testId="account-shares"
        />
      </QueryClientProvider>,
    );
    expect(screen.getByTestId("share-grantee").textContent).toBe("đơn vị team-a");
    expect(screen.getByTestId("share-limits").textContent).toMatch(/^tới A2, ngân sách .*50.*, ngưỡng 80%, 2 run cùng lúc$/);
    expect(screen.getByTestId("share-ends-soon")).toBeTruthy();
    expect(screen.getByTestId("extra")).toBeTruthy();
    expect(screen.queryByTestId("shares-empty")).toBeNull();
  });

  it("says nothing is shared, and why Share is off with nothing to share", () => {
    renderVi(
      <QueryClientProvider client={new QueryClient()}>
        <SharesSection targets={[]} shares={[]} onChanged={() => Promise.resolve()} now={NOW} testId="secret-shares" />
      </QueryClientProvider>,
    );
    expect(screen.getByTestId("shares-empty").textContent).toBe("Chưa chia sẻ gì.");
    expect((screen.getByTestId("share-open") as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByTestId("share-none")).toBeTruthy();
  });
});
