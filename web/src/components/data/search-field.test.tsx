// @vitest-environment jsdom
import { act, fireEvent, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { renderVi } from "@/test/render";

import { SearchField } from "./search-field";
import { takesText } from "./search-shortcut";

/** A field whose committed query comes back as its value, as the URL does on a page. */
function Field({ testId = "search", onCommit = vi.fn(), shortcut }: { testId?: string; onCommit?: (q: string) => void; shortcut?: boolean }) {
  const [value, setValue] = useState("");
  return (
    <SearchField
      value={value}
      onCommit={(q) => {
        setValue(q);
        onCommit(q);
      }}
      label={`Search ${testId}`}
      placeholder="Step, plan or #run"
      clearLabel="Clear search"
      debounce={50}
      shortcut={shortcut}
      testId={testId}
    />
  );
}

beforeEach(() => {
  // jsdom lays nothing out; the shortcut skips fields that take no room, so give every element one box.
  vi.spyOn(Element.prototype, "getClientRects").mockReturnValue([{}] as unknown as DOMRectList);
});

afterEach(() => {
  vi.useRealTimers();
});

describe("SearchField", () => {
  it("shows the / key while it is empty and a clear button once it holds text", async () => {
    const user = userEvent.setup();
    const onCommit = vi.fn();
    renderVi(<Field onCommit={onCommit} />);
    const input = screen.getByRole("searchbox", { name: "Search search" });
    expect(input).toHaveAttribute("aria-keyshortcuts", "/");
    expect(screen.getByText("/").tagName).toBe("KBD");

    await user.type(input, "deploy");
    expect(screen.queryByText("/")).not.toBeInTheDocument();
    await vi.waitFor(() => expect(onCommit).toHaveBeenCalledWith("deploy"));

    await user.click(screen.getByRole("button", { name: "Clear search" }));
    expect(onCommit).toHaveBeenLastCalledWith("");
    expect(input).toHaveValue("");
    expect(input).toHaveFocus();
  });

  it("commits at once on Enter, and empties on Escape", async () => {
    const user = userEvent.setup();
    const onCommit = vi.fn();
    renderVi(<Field onCommit={onCommit} />);
    const input = screen.getByRole("searchbox");
    await user.type(input, "#7{Enter}");
    expect(onCommit).toHaveBeenCalledWith("#7");
    await user.keyboard("{Escape}");
    expect(onCommit).toHaveBeenLastCalledWith("");
    expect(input).toHaveValue("");
  });
});

describe("the / shortcut", () => {
  it("focuses the page's search field when focus is outside any text field", () => {
    renderVi(
      <>
        <button type="button">Elsewhere</button>
        <Field />
      </>,
    );
    screen.getByRole("button", { name: "Elsewhere" }).focus();
    const pressed = fireEvent.keyDown(document.activeElement ?? document.body, { key: "/" });
    expect(pressed).toBe(false); // the slash is not typed anywhere
    expect(screen.getByRole("searchbox")).toHaveFocus();
  });

  it("leaves a key typed into another field alone", () => {
    renderVi(
      <>
        <label>
          Note
          <textarea />
        </label>
        <Field />
      </>,
    );
    const note = screen.getByRole("textbox", { name: "Note" });
    note.focus();
    expect(fireEvent.keyDown(note, { key: "/" })).toBe(true);
    expect(note).toHaveFocus();
  });

  it("ignores / with a modifier, and while a dialog is open", () => {
    renderVi(<Field />);
    fireEvent.keyDown(document.body, { key: "/", metaKey: true });
    expect(screen.getByRole("searchbox")).not.toHaveFocus();

    const dialog = document.createElement("div");
    dialog.setAttribute("role", "dialog");
    dialog.setAttribute("data-state", "open");
    document.body.append(dialog);
    fireEvent.keyDown(document.body, { key: "/" });
    expect(screen.getByRole("searchbox")).not.toHaveFocus();
    dialog.remove();
  });

  it("picks the first field on the page, and none that opted out", () => {
    renderVi(
      <>
        <Field testId="first" />
        <Field testId="second" />
        <Field testId="off" shortcut={false} />
      </>,
    );
    act(() => {
      fireEvent.keyDown(document.body, { key: "/" });
    });
    expect(screen.getByTestId("first")).toHaveFocus();
    expect(screen.getByTestId("off")).not.toHaveAttribute("aria-keyshortcuts");
  });

  it("skips a field hidden behind a modal", () => {
    renderVi(
      <>
        <div aria-hidden="true">
          <Field testId="behind" />
        </div>
        <Field testId="shown" />
      </>,
    );
    fireEvent.keyDown(document.body, { key: "/" });
    expect(screen.getByTestId("shown")).toHaveFocus();
  });

  it("knows which elements take typed text", () => {
    const make = (html: string) => {
      const box = document.createElement("div");
      box.innerHTML = html;
      return box.firstElementChild;
    };
    expect(takesText(make('<input type="text">'))).toBe(true);
    expect(takesText(make('<input type="date">'))).toBe(true);
    expect(takesText(make('<input type="checkbox">'))).toBe(false);
    expect(takesText(make("<select></select>"))).toBe(true);
    expect(takesText(make('<div role="combobox"></div>'))).toBe(true);
    expect(takesText(make("<button></button>"))).toBe(false);
  });
});
