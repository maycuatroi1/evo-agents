// @vitest-environment jsdom
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { SearchX } from "lucide-react";
import { useState } from "react";
import { describe, expect, it } from "vitest";

import { EmptyState, TableSkeleton } from "@/components/states/states";
import { renderVi } from "@/test/render";

import { DataCard, DataToolbar } from "./data-card";
import { CellMain, DataTable, dataTableColumns } from "./data-table";
import { FacetGroup } from "./facet-group";

type Row = { id: number; title: string; where: string; size: number };

const rows: Row[] = [
  { id: 1, title: "Web shell", where: "agent-hub, step 24", size: 120 },
  { id: 2, title: "Bảng theo kit", where: "hub-ui-kit, step 5", size: 9 },
];

function columns() {
  const helper = dataTableColumns<Row>();
  return helper.columns([
    helper.accessor("title", {
      header: () => "Step",
      sortFn: "text",
      meta: { primary: true },
      cell: (info) => (
        <CellMain sub={info.row.original.where}>
          <a href={`#${info.row.original.id}`} className="truncate">
            {info.getValue()}
          </a>
        </CellMain>
      ),
    }),
    helper.accessor("size", { header: () => "Size", sortFn: "basic", meta: { numeric: true }, cell: (info) => info.getValue() }),
    helper.display({
      id: "actions",
      header: () => <span className="sr-only">Actions</span>,
      meta: { actions: true },
      cell: (info) => <button type="button">Open {info.row.original.id}</button>,
    }),
  ]);
}

describe("DataTable", () => {
  it("draws the kit's header, 44 px rows, numbers on the right and actions that show on hover or focus", () => {
    renderVi(<DataTable data={rows} columns={columns()} caption="Runs" testId="table" />);
    const table = screen.getByRole("table", { name: "Runs" });
    const [step, size] = within(table).getAllByRole("columnheader");
    expect(step.className).toContain("bg-surface-sunken");
    expect(step.className).toContain("w-full"); // the title column takes the width the others leave
    expect(size.className).toContain("text-right");

    const cells = within(within(table).getAllByRole("row")[1]).getAllByRole("cell");
    expect(cells[0].className).toContain("h-11");
    expect(cells[1].className).toContain("tabular-nums");
    expect(cells[1].className).toContain("text-right");
    expect(within(cells[2]).getByRole("button", { name: "Open 1" }).parentElement).toHaveClass("row-actions");
    // The title over one secondary line.
    expect(within(cells[0]).getByRole("link", { name: "Web shell" })).toBeInTheDocument();
    expect(within(cells[0]).getByText("agent-hub, step 24").className).toContain("truncate");
    // Alone, the table is its own card.
    expect(screen.getByTestId("table").className).toContain("rounded-md");
  });

  it("drops to 36 px rows when compact, and sorts by a header with aria-sort", async () => {
    const user = userEvent.setup();
    renderVi(<DataTable data={rows} columns={columns()} caption="Audit" density="compact" />);
    const table = screen.getByRole("table", { name: "Audit" });
    expect(within(within(table).getAllByRole("row")[1]).getAllByRole("cell")[0].className).toContain("h-9");
    const sort = within(table).getByRole("button", { name: /Size/ });
    await user.click(sort); // numbers sort largest first
    expect(within(table).getAllByRole("columnheader")[1]).toHaveAttribute("aria-sort", "descending");
    expect(within(within(table).getAllByRole("row")[1]).getByRole("link")).toHaveTextContent("Web shell");
    await user.click(sort);
    expect(within(table).getAllByRole("columnheader")[1]).toHaveAttribute("aria-sort", "ascending");
    expect(within(within(table).getAllByRole("row")[1]).getByRole("link")).toHaveTextContent("Bảng theo kit");
  });

  it("puts a row's detail under it, across every column, the two a group of their own", () => {
    const detail = (row: Row) => (row.id === 1 ? <p>Usage of {row.title}</p> : null);
    renderVi(<DataTable data={rows} columns={columns()} caption="Accounts" detail={detail} />);
    const table = screen.getByRole("table", { name: "Accounts" });
    const groups = within(table).getAllByRole("rowgroup").slice(1); // the header first
    expect(groups).toHaveLength(2);
    const [first, second] = groups.map((group) => within(group).getAllByRole("row"));
    expect(first).toHaveLength(2);
    const [cell] = within(first[1]).getAllByRole("cell");
    expect(cell).toHaveAttribute("colspan", "3");
    expect(cell).toHaveTextContent("Usage of Web shell");
    expect(second).toHaveLength(1);
    expect(groups[1]).toHaveAttribute("data-row-group");
  });

  it("keeps what a cell holds when its columns are rebuilt around fresh data", async () => {
    const user = userEvent.setup();
    function Toggle({ id }: { id: number }) {
      const [open, setOpen] = useState(false);
      return (
        <button type="button" onClick={() => setOpen(true)}>
          {open ? `Dialog ${id} open` : `Open ${id}`}
        </button>
      );
    }
    // A page that refreshes its runs every few seconds builds new columns, with new cell functions, on each refresh.
    function Refreshing() {
      const [refreshes, setRefreshes] = useState(0);
      const helper = dataTableColumns<Row>();
      const refreshed = helper.columns([
        helper.accessor("title", { header: () => "Step", meta: { primary: true }, cell: (info) => `${info.getValue()} ${refreshes}` }),
        helper.display({
          id: "actions",
          header: () => <span className="sr-only">Actions</span>,
          meta: { actions: true },
          cell: (info) => <Toggle id={info.row.original.id} />,
        }),
      ]);
      return (
        <>
          <button type="button" onClick={() => setRefreshes((count) => count + 1)}>
            Refresh
          </button>
          <DataTable data={rows.map((row) => ({ ...row }))} columns={refreshed} caption="Plans" getRowId={(row) => String(row.id)} />
        </>
      );
    }
    renderVi(<Refreshing />);
    await user.click(screen.getByRole("button", { name: "Open 1" }));
    await user.click(screen.getByRole("button", { name: "Refresh" }));
    expect(screen.getByRole("cell", { name: "Web shell 1" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Dialog 1 open" })).toBeInTheDocument();
  });
});

describe("DataCard", () => {
  it("holds the toolbar, its count and the list, and the parts inside draw no frame of their own", () => {
    renderVi(
      <DataCard
        testId="card"
        toolbar={
          <DataToolbar label="Runs of project demo" count="2 runs" countTestId="count">
            <FacetGroup
              label="State"
              options={[
                { value: null, label: "All", count: 2 },
                { value: "done", label: "Done", count: 1 },
              ]}
              selected={null}
              onSelect={() => undefined}
              countLabel={(count) => `${count} runs`}
            />
          </DataToolbar>
        }
      >
        <DataTable data={rows} columns={columns()} caption="Runs" testId="table" />
        <EmptyState icon={SearchX} title="Nothing here" />
        <TableSkeleton rows={1} />
      </DataCard>,
    );
    const toolbar = screen.getByRole("search", { name: "Runs of project demo" });
    expect(within(toolbar).getByTestId("count")).toHaveTextContent("2 runs");
    expect(within(toolbar).getByTestId("count")).toHaveAttribute("aria-live", "polite");
    const chips = within(toolbar).getByRole("group", { name: "State" });
    expect(within(chips).getByRole("button", { name: "All 2 runs" })).toHaveAttribute("aria-pressed", "true");
    expect(within(chips).getByRole("button", { name: "Done 1 runs" })).toHaveAttribute("aria-pressed", "false");
    expect(screen.getByTestId("table").className).not.toContain("rounded-md");
    expect(screen.getByTestId("state-empty").className).not.toContain("border");
  });
});
