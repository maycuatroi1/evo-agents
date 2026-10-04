// @vitest-environment jsdom
import { act, renderHook, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { renderVi } from "@/test/render";

import { Pager } from "./pager";
import { useCursorTrail } from "./url-state";

const handlers = () => ({ onNext: vi.fn(), onPrevious: vi.fn(), onFirst: vi.fn() });

describe("Pager", () => {
  it("is not shown for a list that fits on one page", () => {
    renderVi(<Pager label="Audit" page={1} count={3} hasNext={false} canGoBack={false} atStart busy={false} {...handlers()} />);
    expect(screen.queryByRole("navigation")).not.toBeInTheDocument();
  });

  it("names the page and keeps an unavailable button focusable instead of disabling it", async () => {
    const user = userEvent.setup();
    const on = handlers();
    renderVi(<Pager label="Audit" page={2} count={2} hasNext={false} canGoBack atStart={false} busy={false} {...on} />);
    expect(screen.getByRole("navigation", { name: "Phân trang Audit" })).toHaveTextContent("Trang 2");
    const next = screen.getByRole("button", { name: "Trang sau" });
    expect(next).toHaveAttribute("aria-disabled", "true");
    expect(next).toBeEnabled();
    await user.click(next);
    expect(on.onNext).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Trang trước" }));
    expect(on.onPrevious).toHaveBeenCalledOnce();
  });

  it("offers the first page when it knows no way back", async () => {
    const user = userEvent.setup();
    const on = handlers();
    renderVi(<Pager label="Token" page={null} count={50} hasNext canGoBack={false} atStart={false} busy={false} {...on} />);
    expect(screen.queryByRole("button", { name: "Trang trước" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Về trang đầu" }));
    expect(on.onFirst).toHaveBeenCalledOnce();
  });

  it("says a page is loading and ignores clicks meanwhile", async () => {
    const user = userEvent.setup();
    const on = handlers();
    renderVi(<Pager label="Audit" page={1} count={25} hasNext canGoBack={false} atStart busy {...on} />);
    expect(screen.getByRole("navigation")).toHaveTextContent("Đang tải trang…");
    await user.click(screen.getByRole("button", { name: "Trang sau" }));
    expect(on.onNext).not.toHaveBeenCalled();
  });
});

describe("useCursorTrail", () => {
  it("remembers the cursors behind the current page, per set of filters", () => {
    const { result, rerender } = renderHook(({ key, cursor }) => useCursorTrail(key, cursor), {
      initialProps: { key: "actor=octo", cursor: "" },
    });
    expect(result.current).toMatchObject({ page: 1, canGoBack: false });
    act(() => result.current.forward());
    rerender({ key: "actor=octo", cursor: "c1" });
    expect(result.current).toMatchObject({ page: 2, canGoBack: true });
    act(() => result.current.forward());
    rerender({ key: "actor=octo", cursor: "c2" });
    expect(result.current.page).toBe(3);

    let previous = "";
    act(() => {
      previous = result.current.back();
    });
    expect(previous).toBe("c1");
    rerender({ key: "actor=octo", cursor: "c1" });
    expect(result.current.page).toBe(2);

    // Other filters, or a page opened from a link: no way back is known.
    rerender({ key: "actor=hubot", cursor: "c9" });
    expect(result.current).toMatchObject({ page: null, canGoBack: false });
  });
});
