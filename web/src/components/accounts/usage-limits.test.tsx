// @vitest-environment jsdom
import { screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { renderVi } from "@/test/render";

import type { Account } from "./queries";
import { UsageLimits } from "./usage-limits";

const NOW = Date.parse("2026-10-14T04:09:00Z");

const ACCOUNT: Account = {
  name: "claude-a",
  env_var: "CLAUDE_CODE_OAUTH_TOKEN",
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
  runs_24h: 0,
  runs_7d: 0,
  cost_24h_usd: 0,
  cost_7d_usd: 0,
  held_by: [],
};

describe("an account's usage limits", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(NOW);
  });
  afterEach(() => vi.useRealTimers());

  it("draws a named bar per window with its share, its reset and the age of the report", () => {
    const usage: Account["last_usage"] = {
      status: "rejected",
      rate_limit_type: "seven_day_opus",
      windows: {
        five_hour: { utilization: 0.35, resets_at: "2026-10-14T07:00:00Z" },
        seven_day: { utilization: 0.83, resets_at: "2026-10-20T00:00:00Z" },
        seven_day_opus: { utilization: 1, resets_at: "2026-10-20T00:00:00Z" },
      },
    };
    renderVi(<UsageLimits account={{ ...ACCOUNT, last_usage: usage, last_usage_at: "2026-10-14T04:04:00Z" }} pageNow={NOW} />);
    expect(screen.getByRole("group", { name: "Giới hạn sử dụng của claude-a" })).toHaveTextContent("báo cáo 5 phút trước");
    const session = screen.getByRole("progressbar", { name: "Phiên (5 giờ)" });
    expect(session).toHaveAttribute("aria-valuenow", "35");
    expect(session).toHaveAttribute("data-tone", "normal");
    expect(screen.getByText("Đặt lại sau 2 giờ 51 phút")).toBeInTheDocument();
    expect(screen.getByRole("progressbar", { name: "Hằng tuần, mọi model" })).toHaveAttribute("data-tone", "warning");
    const opus = screen.getByRole("progressbar", { name: "Hằng tuần, Opus" });
    expect(opus).toHaveAttribute("data-tone", "danger");
    expect(opus).toHaveAttribute("aria-valuetext", "Đã chạm giới hạn");
  });

  it("says once that nothing was reported yet, without a bar", () => {
    renderVi(<UsageLimits account={ACCOUNT} pageNow={NOW} />);
    expect(screen.getByTestId("account-usage")).toHaveTextContent("Chưa có báo cáo mức dùng");
    expect(screen.queryByRole("progressbar")).toBeNull();
  });
});
