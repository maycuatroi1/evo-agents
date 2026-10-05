// @vitest-environment jsdom
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { renderVi } from "@/test/render";

import { RunLogCard } from "./run-log";
import { RunTerminalPanel } from "./run-terminal";
import type { RunLog } from "./use-run-log";

vi.mock("@/lib/api/browser", () => ({
  browserApi: () => {
    throw new Error("the panel reads nothing before Connect");
  },
}));

const run = { id: 12, project: "demo", state: "interactive" as const, mode: "headless" as const, worker: "laptop" };
const fresh = () => new Date(Date.now() - 60_000).toISOString();

describe("RunTerminalPanel", () => {
  it("waits for Connect, and says what connecting to an interactive run does", () => {
    renderVi(<RunTerminalPanel run={run} open active sessionCreatedAt={fresh()} />);
    expect(screen.getByTestId("terminal-status")).toHaveTextContent("Chưa kết nối");
    expect(screen.getByTestId("terminal-status").closest('[role="status"]')).not.toBeNull();
    expect(screen.getByTestId("terminal-intro")).toHaveTextContent("TUI của agent đang chạy trong tmux trên laptop");
    expect(screen.getByTestId("terminal-connect")).toBeEnabled();
    expect(screen.getByTestId("terminal-connect")).toHaveTextContent("Kết nối terminal");
    expect(screen.queryByTestId("terminal-message")).toBeNull();
  });

  it("warns that connecting to a headless run takes it over", () => {
    renderVi(<RunTerminalPanel run={{ ...run, state: "running" }} open active sessionCreatedAt={fresh()} />);
    expect(screen.getByTestId("terminal-intro")).toHaveTextContent("Kết nối nghĩa là tiếp quản run");
  });

  it("attaches to a run dispatched interactive, even before the worker says it is", () => {
    renderVi(<RunTerminalPanel run={{ ...run, state: "leased", mode: "interactive" }} open active sessionCreatedAt={fresh()} />);
    expect(screen.getByTestId("terminal-intro")).toHaveTextContent("TUI của agent đang chạy trong tmux trên laptop");
  });

  it("offers no Connect while the run is in a state no terminal opens in", () => {
    renderVi(<RunTerminalPanel run={{ ...run, state: "verifying" }} open={false} active sessionCreatedAt={fresh()} />);
    expect(screen.getByTestId("terminal-connect")).toBeDisabled();
    expect(screen.getByTestId("terminal-message")).toHaveTextContent(
      "Run #12 đang ở trạng thái Đang kiểm: terminal chỉ mở khi run ở trạng thái Đã nhận, Đang chạy hoặc Tương tác.",
    );
  });

  it("asks to sign in again when the sign-in is older than 12 hours", async () => {
    const old = new Date(Date.now() - 13.5 * 3_600_000).toISOString();
    renderVi(<RunTerminalPanel run={run} open active sessionCreatedAt={old} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("Bạn đăng nhập cách đây 13 giờ");
    expect(screen.getByTestId("terminal-sign-in")).toHaveTextContent("Đăng nhập lại");
    expect(screen.queryByTestId("terminal-connect")).toBeNull();
  });
});

const log: RunLog = { lines: [], moves: [], status: "live" } as unknown as RunLog;

describe("RunLogCard with a terminal", () => {
  it("is the log alone without one", () => {
    renderVi(<RunLogCard runId={12} log={log} active composer={null} />);
    expect(screen.queryByRole("tablist")).toBeNull();
    expect(screen.getByRole("heading", { level: 2 })).toHaveTextContent("Log");
  });

  it("puts the log and the terminal under two tabs, both kept mounted", async () => {
    const user = userEvent.setup();
    const panel = vi.fn((shown: boolean) => <p data-testid="panel" data-shown={shown} />);
    renderVi(<RunLogCard runId={12} log={log} active composer={null} terminal={panel} />);
    const tabs = within(screen.getByRole("tablist", { name: "Cách xem phiên" })).getAllByRole("tab");
    expect(tabs.map((tab) => tab.textContent)).toEqual(["Log", "Terminal"]);
    expect(tabs[0]).toHaveAttribute("aria-selected", "true");
    expect(screen.getByTestId("panel")).toHaveAttribute("data-shown", "false");
    expect(screen.getByTestId("log-lines")).toBeVisible();

    await user.click(tabs[1]);
    expect(tabs[1]).toHaveAttribute("aria-selected", "true");
    expect(screen.getByTestId("panel")).toHaveAttribute("data-shown", "true");
    expect(screen.getByTestId("log-lines")).toBeInTheDocument(); // hidden, not unmounted
    expect(screen.queryByTestId("log-count")).toBeNull();
  });
});
