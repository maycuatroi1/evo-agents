// @vitest-environment jsdom
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import Link from "next/link";
import { describe, expect, it, vi } from "vitest";

import { ReadOnlyNotice } from "@/components/plans/plan-header";
import { renderVi } from "@/test/render";

import { PageHeader } from "./page-header";

describe("PageHeader", () => {
  it("puts the h1, its state, its tags and the actions in one row, with an optional line under it", () => {
    renderVi(
      <PageHeader
        title="Run #12"
        status={<span data-testid="status">Running</span>}
        tags={<span data-testid="tag">Plan run</span>}
        actions={<button type="button">Cancel</button>}
        sub={<Link href="/p/demo/plans/agent-hub">agent-hub</Link>}
        testId="head"
      />,
    );
    const head = screen.getByTestId("head");
    const heading = within(head).getByRole("heading", { level: 1, name: "Run #12" });
    // The title is set in the interface face at the kit's page-title size, never in mono.
    expect(heading.className).toContain("text-xl");
    expect(heading.className).not.toContain("font-mono");
    const row = heading.parentElement?.parentElement;
    expect(row).toContainElement(screen.getByTestId("status"));
    expect(row).toContainElement(screen.getByTestId("tag"));
    expect(row).toContainElement(screen.getByRole("button", { name: "Cancel" }));
    expect(head.querySelector("[data-slot=page-actions]")?.className).toContain("ml-auto");
    expect(head.querySelector("[data-slot=page-sub]")).toContainElement(screen.getByRole("link", { name: "agent-hub" }));
    // No eyebrow, description paragraph or rule under the head.
    expect(head.className).not.toContain("border-b");
    expect(head.querySelectorAll("p")).toHaveLength(1);
  });

  it("leaves out the actions and the line under the row when a page has none", () => {
    renderVi(<PageHeader title="Workers" testId="head" />);
    const head = screen.getByTestId("head");
    expect(head.querySelector("[data-slot=page-actions]")).toBeNull();
    expect(head.querySelector("[data-slot=page-sub]")).toBeNull();
  });
});

describe("ReadOnlyNotice", () => {
  it("says in one sentence how a plan changes, and copies the command", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    const user = userEvent.setup();
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    renderVi(<ReadOnlyNotice />);
    const banner = screen.getByRole("note", { name: "Chỉ đọc" });
    expect(banner).toHaveTextContent("Chỉ đọc. Plan được sửa từ CLI bằng evo harness step; mỗi lần sửa thêm một revision.");
    const copy = within(banner).getByRole("button", { name: "Sao chép lệnh evo harness step" });
    await user.click(copy);
    expect(writeText).toHaveBeenCalledWith("evo harness step");
    expect(copy).toHaveTextContent("Đã chép");
    expect(await screen.findByText("Đã chép evo harness step vào clipboard")).toBeInTheDocument();
  });

  it("selects the command where the clipboard is refused, and says how to copy it", async () => {
    const user = userEvent.setup();
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText: vi.fn().mockRejectedValue(new Error("denied")) },
      configurable: true,
    });
    renderVi(<ReadOnlyNotice />);
    await user.click(screen.getByRole("button", { name: "Sao chép lệnh evo harness step" }));
    expect(window.getSelection()?.toString()).toBe("evo harness step");
    expect(await screen.findByText("Đã chọn sẵn: bấm Ctrl+C hoặc Cmd+C để chép")).toBeInTheDocument();
  });
});
