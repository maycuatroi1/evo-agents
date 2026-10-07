// @vitest-environment jsdom
import { screen, within } from "@testing-library/react";
import type { Route } from "next";
import { afterEach, describe, expect, it, vi } from "vitest";

import { renderVi } from "@/test/render";

import { DataCard } from "./data-card";
import { CellMain, DataTable, dataTableColumns } from "./data-table";

type Row = { id: number; title: string; where: string; state: string; error: string | null };

const rows: Row[] = [
  { id: 7, title: "Run dài để kill daemon", where: "worker-smoke, step 5", state: "Done", error: null },
  { id: 4, title: "Claude Code, take over rồi hand back", where: "worker-smoke, step 4", state: "Failed", error: "The agent did not write result.json" },
];

function columns() {
  const helper = dataTableColumns<Row>();
  return helper.columns([
    helper.accessor("id", { header: () => "Run", sortFn: "basic", cell: (info) => `#${info.getValue()}` }),
    helper.accessor("title", {
      header: () => "Step",
      sortFn: "text",
      meta: { primary: true },
      cell: (info) => <CellMain sub={info.row.original.where}>{info.getValue()}</CellMain>,
    }),
    helper.accessor("state", { header: () => "State", cell: (info) => info.getValue() }),
  ]);
}

/** A phone's 375 px screen, or a laptop's, as `useIsMobile` reads it. */
function screenWidth(phone: boolean) {
  vi.stubGlobal(
    "matchMedia",
    vi.fn(() => ({ matches: phone, addEventListener: vi.fn(), removeEventListener: vi.fn() })),
  );
}

function phoneRow(row: Row) {
  return {
    title: row.title,
    href: `/runs/${row.id}` as Route,
    status: <span data-testid="pill">{row.state}</span>,
    meta: row.error ?? `#${row.id}, ${row.where}`,
    danger: row.error !== null,
    actions: <button type="button">Rerun {row.id}</button>,
    data: { "run-id": row.id },
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("DataTable under 768 px", () => {
  it("lists the rows instead: a link to each object's page, its state and one meta line", () => {
    screenWidth(true);
    renderVi(
      <DataTable
        data={rows}
        columns={columns()}
        caption="Runs"
        testId="runs"
        initialSorting={[{ id: "id", desc: false }]}
        mobile={phoneRow}
      />,
    );
    expect(screen.queryByRole("table")).toBeNull();
    const frame = screen.getByTestId("runs");
    expect(frame).toHaveAttribute("data-layout", "list");
    const list = within(frame).getByRole("list", { name: "Runs" });
    const items = within(list).getAllByRole("listitem");
    // The table's order: sorted by run, #4 first.
    expect(items.map((item) => item.getAttribute("data-run-id"))).toEqual(["4", "7"]);

    const link = within(items[0]).getByRole("link", { name: "Claude Code, take over rồi hand back" });
    expect(link).toHaveAttribute("href", "/runs/4");
    // The link's target covers the row, a touch target of at least 60 px.
    expect(link.className).toContain("after:absolute");
    expect(items[0].className).toContain("min-h-[60px]");
    // The state and the meta line describe the link; a failure is in danger.
    const described = (link.getAttribute("aria-describedby") ?? "").split(" ").map((id) => document.getElementById(id));
    expect(described.map((node) => node?.textContent)).toEqual(["Failed", "The agent did not write result.json"]);
    expect(described[1]).toHaveClass("text-danger", "truncate");
    // Other buttons in the row sit above the link's cover.
    expect(within(items[0]).getByRole("button", { name: "Rerun 4" })).toBeInTheDocument();
    expect(items[0].className).toContain("[&_:is(a,button):not([data-row-link])]:relative");
  });

  it("keeps a table without a phone row a table, and a table on a wide screen", () => {
    screenWidth(true);
    const { unmount } = renderVi(<DataTable data={rows} columns={columns()} caption="Runs" testId="runs" />);
    expect(screen.getByRole("table", { name: "Runs" })).toBeInTheDocument();
    expect(screen.getByTestId("runs")).toHaveAttribute("data-layout", "table");
    unmount();

    screenWidth(false);
    renderVi(<DataTable data={rows} columns={columns()} caption="Runs" testId="runs" mobile={phoneRow} />);
    expect(screen.getByRole("table", { name: "Runs" })).toBeInTheDocument();
    expect(screen.queryByRole("list")).toBeNull();
  });

  it("draws no frame inside a card, rows without a page link nowhere, and says when nothing matches", () => {
    screenWidth(true);
    const { unmount } = renderVi(
      <DataCard>
        <DataTable
          data={rows}
          columns={columns()}
          caption="Audit"
          testId="audit"
          density="compact"
          mobile={(row) => ({ title: row.title, meta: row.where })}
        />
      </DataCard>,
    );
    const frame = screen.getByTestId("audit");
    expect(frame.className).not.toContain("border");
    expect(within(frame).queryByRole("link")).toBeNull();
    expect(within(frame).getAllByRole("listitem")[0].className).toContain("min-h-[52px]");
    unmount();

    renderVi(<DataTable data={[]} columns={columns()} caption="Runs" empty="No run matches." mobile={phoneRow} />);
    expect(screen.getByText("No run matches.")).toBeInTheDocument();
    expect(screen.queryByRole("list")).toBeNull();
  });
});
