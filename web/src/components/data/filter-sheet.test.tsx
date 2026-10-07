// @vitest-environment jsdom
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { FilterBar, FilterField } from "@/components/admin/filter-bar";
import { renderVi } from "@/test/render";

import { DataToolbar } from "./data-card";
import { FacetGroup } from "./facet-group";
import { ToolbarField, ToolbarFilters } from "./filter-sheet";

function screenWidth(phone: boolean) {
  vi.stubGlobal(
    "matchMedia",
    vi.fn(() => ({ matches: phone, addEventListener: vi.fn(), removeEventListener: vi.fn() })),
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

/** A runs toolbar with one facet, its state held here as the URL holds it in the app. */
function Runs() {
  const [state, setState] = useState<string | null>(null);
  return (
    <DataToolbar label="Runs" count={state ? "1 run matches" : "3 runs"}>
      <ToolbarFilters active={state ? 1 : 0} summary={state ? "1 run matches" : "3 runs"} onClear={() => setState(null)} testId="runs-filters">
        <FacetGroup
          label="State"
          options={[
            { value: null, label: "All", count: 3 },
            { value: "failed", label: "Failed", count: 1 },
          ]}
          selected={state}
          onSelect={setState}
          countLabel={(n) => `${n} runs`}
          testId="runs-facets"
        />
        <ToolbarField label="Project">
          {(id) => (
            <select id={id} defaultValue="">
              <option value="">All projects</option>
            </select>
          )}
        </ToolbarField>
      </ToolbarFilters>
    </DataToolbar>
  );
}

describe("ToolbarFilters", () => {
  it("keeps the filters inline from 768 px, a select labelled for screen readers only", () => {
    screenWidth(false);
    renderVi(<Runs />);
    const group = screen.getByRole("group", { name: "State" });
    expect(within(group).getByRole("button", { name: /Failed/ })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Bộ lọc/ })).toBeNull();
    expect(screen.getByText("Project")).toHaveClass("sr-only");
  });

  it("folds them under 768 px into Filters (n), a sheet that applies each choice and clears them", async () => {
    screenWidth(true);
    const user = userEvent.setup();
    renderVi(<Runs />);
    expect(screen.queryByRole("group", { name: "State" })).toBeNull();
    const open = screen.getByRole("button", { name: "Bộ lọc" });
    expect(open).toHaveAttribute("aria-haspopup", "dialog");

    await user.click(open);
    const sheet = screen.getByRole("dialog", { name: "Bộ lọc" });
    // Each group's label shows above its choices; a select's label too.
    expect(within(sheet).getByText("State", { selector: "span" })).not.toHaveClass("sr-only");
    expect(within(sheet).getByLabelText("Project")).toBeInTheDocument();
    await user.click(within(sheet).getByRole("button", { name: /Failed/ }));
    expect(within(sheet).getByRole("button", { name: /Failed/ })).toHaveAttribute("aria-pressed", "true");
    expect(within(sheet).getByTestId("filter-sheet-summary")).toHaveTextContent("1 run matches");

    await user.click(within(sheet).getByRole("button", { name: "Xem kết quả" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    const pressed = screen.getByRole("button", { name: "Bộ lọc (1)" });
    expect(pressed).toHaveAttribute("data-active", "1");
    expect(pressed).toHaveFocus();

    await user.click(pressed);
    await user.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Xoá bộ lọc" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.getByRole("button", { name: "Bộ lọc" })).toHaveAttribute("data-active", "0");
  });
});

describe("FilterBar under 768 px", () => {
  function Audit({ onApply }: { onApply: (form: FormData) => boolean | void }) {
    return (
      <FilterBar
        label="Audit"
        onApply={onApply}
        onClear={() => {}}
        canClear={false}
        active={0}
        resetKey="{}"
        count="12 rows"
        testId="audit-filters"
      >
        <FilterField id="actor" label="Actor">
          <input id="actor" name="actor" />
        </FilterField>
      </FilterBar>
    );
  }

  it("puts the form in the sheet, applies it with Filter and closes, or stays open on a wrong field", async () => {
    screenWidth(true);
    const user = userEvent.setup();
    const applied: string[] = [];
    let accept = false;
    renderVi(
      <Audit
        onApply={(form) => {
          applied.push(String(form.get("actor")));
          return accept;
        }}
      />,
    );
    expect(screen.getByTestId("audit-filters-count")).toHaveTextContent("12 rows");
    expect(screen.queryByRole("search")).toBeNull();

    await user.click(screen.getByRole("button", { name: "Bộ lọc" }));
    const form = within(screen.getByRole("dialog")).getByRole("search", { name: "Audit" });
    await user.type(within(form).getByLabelText("Actor"), "not a login");
    await user.click(within(form).getByRole("button", { name: "Lọc" }));
    expect(applied).toEqual(["not a login"]);
    expect(screen.getByRole("dialog")).toBeInTheDocument(); // the page said the field is wrong

    accept = true;
    await user.click(within(form).getByRole("button", { name: "Lọc" }));
    expect(applied).toEqual(["not a login", "not a login"]);
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});
