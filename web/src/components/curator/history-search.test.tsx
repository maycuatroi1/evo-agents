// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeAll, describe, expect, it, vi } from "vitest";

import { createApiClient } from "@/lib/api/client";
import { renderVi } from "@/test/render";

import { NO_HISTORY_FILTERS, searchQueryOf } from "./history-model";
import { HistorySearchSection } from "./history-search";
import { type HistorySearch, insightKeys, type TranscriptLines } from "./insight-queries";
import { TrendCard } from "./trend-card";
import { series } from "./trend-model";

vi.mock("@/lib/api/browser", () => ({
  browserApi: () =>
    createApiClient({
      baseUrl: "http://hub.test",
      fetch: async () => Response.json({ error: "not_found", message: "no such thing" }, { status: 404 }),
    }),
}));

beforeAll(() => {
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  );
});

const FILTERS = { ...NO_HISTORY_FILTERS, q: "tail" };

const FOUND: HistorySearch = {
  project: "demo",
  query: "tail",
  sources: ["transcript", "run"],
  total: 2,
  limit: 20,
  offset: 0,
  reranked: false,
  hits: [
    {
      source: "transcript",
      at: "2026-10-18T03:00:00Z",
      role: "call Bash",
      text: "sleep 30 && «tail» -f build.log",
      rank: 0.9,
      evidence: "transcript:s-1:12",
      session_id: "s-1",
      file: "main",
      line: 12,
      run_id: null,
      run_kind: null,
      chat_id: null,
      chat: null,
      login: "octo",
      runtime: "claude-code",
    },
    {
      source: "run",
      at: "2026-10-18T04:00:00Z",
      role: "agent",
      text: "the «tail» of the log says it passed",
      rank: 0.5,
      evidence: "run:41:7",
      session_id: null,
      file: null,
      line: 7,
      run_id: 41,
      run_kind: "plan",
      chat_id: null,
      chat: null,
      login: "octo",
      runtime: "claude-code",
    },
  ],
};

const LINES: TranscriptLines = {
  project: "demo",
  session_id: "s-1",
  file: "main",
  runtime: "claude-code",
  login: "octo",
  lines: 40,
  asked: 12,
  first: 4,
  last: 20,
  evidence: "transcript:s-1:12",
  entries: [
    { line: 11, role: "agent", text: "I will wait for the build." },
    { line: 12, role: "call Bash", text: "sleep 30 && tail -f build.log" },
    { line: 13, role: "Bash failed", text: "Blocked: sleep followed by tail is not allowed" },
  ],
};

function setup() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
  client.setQueryData(insightKeys.search("demo", searchQueryOf(FILTERS)), FOUND);
  client.setQueryData(insightKeys.window("demo", "s-1", "main", 12, 8, 8), LINES);
  const onFilters = vi.fn();
  renderVi(
    <QueryClientProvider client={client}>
      <HistorySearchSection project="demo" filters={FILTERS} onFilters={onFilters} />
    </QueryClientProvider>,
  );
  return { user: userEvent.setup(), onFilters };
}

describe("HistorySearchSection", () => {
  it("lists the hits best first, the matched words marked, each leading where it was found", async () => {
    const { user } = setup();
    const hits = screen.getAllByTestId("history-hit");
    expect(hits.map((hit) => hit.getAttribute("data-source"))).toEqual(["transcript", "run"]);
    expect(within(hits[0]).getByText("tail", { selector: "mark" })).toBeInTheDocument();
    expect(screen.getByTestId("history-search-total")).toHaveTextContent("2 kết quả");
    expect(within(hits[1]).getByTestId("history-hit-run")).toHaveAttribute("href", "/p/demo/runs/41?view=log");
    await user.click(within(hits[0]).getByTestId("history-hit-lines"));
    const excerpt = within(hits[0]).getByTestId("history-hit-excerpt");
    expect(within(excerpt).getByTestId("history-hit-excerpt-span")).toHaveTextContent("Dòng 4 đến 20 trong 40 dòng của main");
    const asked = excerpt.querySelector("[data-asked]");
    expect(asked).toHaveAttribute("data-line", "12");
    expect(within(excerpt).getByText("Bash failed")).toHaveClass("text-danger");
  });

  it("filters by source and starts the hits again from the first page", async () => {
    const { user, onFilters } = setup();
    await user.click(within(screen.getByTestId("history-search-sources")).getByRole("button", { name: /Log của run/ }));
    expect(onFilters).toHaveBeenLastCalledWith({ ...FILTERS, sources: ["run"], hits: 1 });
  });
});

describe("TrendCard", () => {
  const rows = [
    { at: "2026-10-17", tick: "17", title: "Oct 17", values: { a: 1, b: null } },
    { at: "2026-10-18", tick: "18", title: "Oct 18", values: { a: 3, b: 2 } },
  ];
  const keys = series([
    { key: "a", label: "First" },
    { key: "b", label: "Second" },
  ]);

  it("says so in one line when no row holds a value", () => {
    renderVi(<TrendCard title="Cost" headline="unused" empty="Nothing named its cost." kind="lines" rows={[]} series={keys} format={String} testId="trend" />);
    expect(screen.getByTestId("trend-headline")).toHaveTextContent("Nothing named its cost.");
    expect(screen.queryByTestId("trend-view-table")).toBeNull();
  });

  it("keeps the table of a stack for screen readers, the newest row first with its total, and shows it when asked", async () => {
    renderVi(<TrendCard title="Context" headline="Per session" empty="none" kind="bars" rows={rows} series={keys} format={String} testId="trend" />);
    const hidden = screen.getByTestId("trend-table");
    expect(hidden).toHaveAttribute("data-visible", "false");
    const lines = within(hidden).getAllByTestId("trend-row");
    expect(lines.map((row) => row.getAttribute("data-at"))).toEqual(["2026-10-18", "2026-10-17"]);
    expect(within(lines[1]).getAllByRole("cell").map((cell) => cell.textContent)).toEqual(["1", "Không có", "1"]);
    await userEvent.setup().click(screen.getByTestId("trend-view-table"));
    expect(screen.getByRole("region", { name: "Context, dạng bảng" })).toHaveAttribute("tabindex", "0");
  });
});
