// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { createApiClient } from "@/lib/api/client";
import { renderVi } from "@/test/render";

import { InsightsPage } from "./insights-page";
import { type InsightRange, type RunDay, type RunStats, statsKey } from "./queries";

const nav = vi.hoisted(() => ({ search: "" }));

vi.mock("@/lib/api/browser", () => ({
  browserApi: () =>
    createApiClient({
      baseUrl: "http://hub.test",
      fetch: async () => Response.json({ error: "not_found", message: "no such thing" }, { status: 404 }),
    }),
}));

vi.mock("next/navigation", () => ({
  usePathname: () => "/p/demo/insights",
  useSearchParams: () => new URLSearchParams(nav.search),
  useRouter: () => ({ push: vi.fn() }),
}));

beforeAll(() => {
  // Recharts measures its container; jsdom has no layout.
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  );
});

beforeEach(() => {
  nav.search = "";
});

function day(date: string, figures: Partial<RunDay> = {}): RunDay {
  return {
    day: date,
    done: 0,
    failed: 0,
    lost: 0,
    cancelled: 0,
    p50_seconds: null,
    p90_seconds: null,
    input_tokens: 0,
    output_tokens: 0,
    cache_read_tokens: 0,
    reasoning_tokens: 0,
    runs_with_usage: 0,
    ...figures,
  };
}

/** Three days: two done and one failed with usage, then a quiet day, then a cancelled run and a done one. */
function stats(days: InsightRange = 30, empty = false): RunStats {
  const byDay = empty
    ? [day("2026-10-05"), day("2026-10-06"), day("2026-10-07")]
    : [
        day("2026-10-05", {
          done: 2,
          failed: 1,
          p50_seconds: 300,
          p90_seconds: 540,
          cache_read_tokens: 17000,
          input_tokens: 4400,
          output_tokens: 1500,
          reasoning_tokens: 400,
          runs_with_usage: 3,
        }),
        day("2026-10-06"),
        day("2026-10-07", { done: 1, cancelled: 1, p50_seconds: 45, p90_seconds: 57 }),
      ];
  const sum = (key: keyof RunDay) => byDay.reduce((total, row) => total + (row[key] as number), 0);
  return {
    project: "demo",
    days,
    first_day: "2026-10-05",
    last_day: "2026-10-07",
    by_day: byDay,
    total: {
      done: sum("done"),
      failed: sum("failed"),
      lost: sum("lost"),
      cancelled: sum("cancelled"),
      p50_seconds: empty ? null : 150,
      p90_seconds: empty ? null : 480,
      input_tokens: sum("input_tokens"),
      output_tokens: sum("output_tokens"),
      cache_read_tokens: sum("cache_read_tokens"),
      reasoning_tokens: sum("reasoning_tokens"),
      runs_with_usage: sum("runs_with_usage"),
    },
  };
}

function setup(data: RunStats, range: InsightRange = 30) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
  client.setQueryData(statsKey("demo", range), data);
  renderVi(
    <QueryClientProvider client={client}>
      <InsightsPage project="demo" initialError={null} />
    </QueryClientProvider>,
  );
  return userEvent.setup();
}

describe("InsightsPage", () => {
  it("heads each chart with what the range adds up to, in the visitor's language", () => {
    setup(stats());
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Thống kê");
    expect(screen.getByTestId("insights-span")).toHaveTextContent("5 thg 10 đến 7 thg 10, theo ngày UTC");
    expect(screen.getByTestId("insights-outcomes-headline")).toHaveTextContent("5 run đã kết thúc: 3 xong, 1 thất bại, 0 mất lease, 1 đã huỷ");
    expect(screen.getByTestId("insights-failure-headline")).toHaveTextContent("25% trong 4 run thất bại hoặc mất lease");
    expect(screen.getByTestId("insights-duration-headline")).toHaveTextContent("Trung vị 2 phút 30 giây, phân vị 90 là 8 phút 0 giây");
    expect(screen.getByTestId("insights-tokens-headline")).toHaveTextContent("23.300 token từ 3 run có báo mức dùng");
  });

  it("keeps a table of every chart for screen readers, the newest day first and the whole range in its foot", () => {
    setup(stats());
    const card = screen.getByTestId("insights-outcomes");
    expect(card).toHaveAttribute("data-view", "chart");
    const table = within(card).getByTestId("insights-outcomes-table");
    expect(table).toHaveAttribute("data-visible", "false");
    expect(table.closest(".sr-only")).not.toBeNull();
    const rows = within(table).getAllByTestId("insights-outcomes-row");
    expect(rows.map((row) => row.getAttribute("data-day"))).toEqual(["2026-10-07", "2026-10-06", "2026-10-05"]);
    expect(within(rows[2]).getAllByRole("cell").map((cell) => cell.textContent)).toEqual(["2", "1", "0", "0", "3"]);
    expect(within(table).getByTestId("insights-outcomes-total")).toHaveTextContent("Cả 3 ngày31015");
    // The legend names the stack's three marks; a chart of one series (the failure rate) has none.
    expect(within(card).getByTestId("insights-outcomes-legend")).toHaveTextContent("XongThất bạiMất lease hoặc đã huỷ");
    expect(screen.queryByTestId("insights-failure-legend")).toBeNull();
  });

  it("shows a chart's table instead of the chart, with no value where a day has none", async () => {
    const user = setup(stats());
    const card = screen.getByTestId("insights-duration");
    await user.click(within(card).getByTestId("insights-duration-view-table"));
    expect(card).toHaveAttribute("data-view", "table");
    expect(within(card).getByTestId("insights-duration-view-table")).toHaveAttribute("aria-pressed", "true");
    const region = within(card).getByRole("region", { name: "Thời lượng run, dạng bảng" });
    expect(region).toHaveAttribute("tabindex", "0");
    const rows = within(region).getAllByTestId("insights-duration-row");
    expect(within(rows[0]).getAllByRole("cell").map((cell) => cell.textContent)).toEqual(["45 giây", "57 giây"]);
    expect(within(rows[1]).getAllByRole("cell").map((cell) => cell.textContent)).toEqual(["Không có", "Không có"]);
    expect(within(rows[2]).getAllByRole("cell").map((cell) => cell.textContent)).toEqual(["5 phút 0 giây", "9 phút 0 giây"]);
    const failure = screen.getByTestId("insights-failure");
    await user.click(within(failure).getByTestId("insights-failure-view-table"));
    const failureRows = within(failure).getAllByTestId("insights-failure-row");
    expect(within(failureRows[0]).getAllByRole("cell").map((cell) => cell.textContent)).toEqual(["0%", "0", "1"]);
    expect(within(failureRows[2]).getAllByRole("cell").map((cell) => cell.textContent)).toEqual(["33%", "1", "3"]);
  });

  it("puts the range in the URL, the 30 days as the plain address", async () => {
    const replace = vi.spyOn(window.history, "replaceState");
    const user = setup(stats());
    const range = screen.getByRole("group", { name: "Khoảng thời gian" });
    expect(within(range).getByTestId("insights-range-30")).toHaveAttribute("aria-pressed", "true");
    await user.click(within(range).getByTestId("insights-range-7"));
    expect(replace).toHaveBeenLastCalledWith(null, "", "/p/demo/insights?days=7");
    await user.click(within(range).getByTestId("insights-range-30"));
    expect(replace).toHaveBeenLastCalledWith(null, "", "/p/demo/insights");
  });

  it("reads the range the URL names", () => {
    nav.search = "days=90";
    setup(stats(90), 90);
    expect(screen.getByTestId("insights-range-90")).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByTestId("insights-outcomes")).toBeInTheDocument();
  });

  it("says no run ended in the range, and offers the runs page and the 90 days", async () => {
    const replace = vi.spyOn(window.history, "replaceState");
    const user = setup(stats(30, true));
    const empty = screen.getByTestId("state-empty");
    expect(empty).toHaveTextContent("Không run nào kết thúc trong 30 ngày qua");
    expect(within(empty).getByRole("link", { name: "Mở trang run" })).toHaveAttribute("href", "/p/demo/runs");
    await user.click(within(empty).getByTestId("insights-empty-longer"));
    expect(replace).toHaveBeenLastCalledWith(null, "", "/p/demo/insights?days=90");
    expect(screen.queryByTestId("insights-charts")).toBeNull();
  });
});
