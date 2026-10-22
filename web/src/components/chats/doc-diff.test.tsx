// @vitest-environment jsdom
import { fireEvent, render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import type { ReactElement } from "react";
import { describe, expect, it } from "vitest";

import { diffLines } from "@/lib/line-diff";

import messages from "../../../messages/vi.json";
import { DocDiff, DocDiffLegend } from "./doc-diff";

// The keys of chats.doc, given here in case messages/vi.json does not have them yet.
const DOC = {
  hiddenLines: "{count, plural, other {# dòng không đổi}}",
  diffLabel: "Thay đổi của tài liệu",
  legendAdded: "dòng thêm",
  legendRemoved: "dòng xóa hoặc thay thế",
  legendSame: "giữ nguyên",
};
const doc: typeof DOC = { ...DOC, ...(messages.chats as { doc?: Partial<typeof DOC> }).doc };
const withDoc = { ...messages, chats: { ...messages.chats, doc } };

function renderVi(ui: ReactElement) {
  return render(
    <NextIntlClientProvider locale="vi" messages={withDoc} timeZone="Asia/Ho_Chi_Minh">
      {ui}
    </NextIntlClientProvider>,
  );
}

const BEFORE = Array.from({ length: 30 }, (_, index) => `line ${index + 1}`);

describe("DocDiff", () => {
  it("signs added and removed rows and marks each row's kind", () => {
    renderVi(<DocDiff lines={diffLines("a\nb\nc", "a\nB\nc\nd")} />);
    const rows = screen.getByTestId("doc-diff").querySelectorAll("[data-kind]");
    expect(Array.from(rows, (row) => row.getAttribute("data-kind"))).toEqual(["same", "del", "add", "same", "add"]);
    expect(rows[1]).toHaveTextContent("2−b");
    expect(rows[2]).toHaveTextContent("2+B");
    expect(rows[1].className).toContain("bg-red-500/15");
    expect(rows[2].className).toContain("bg-green-500/15");
    expect(rows[0].className).not.toContain("bg-");
  });

  it("folds a long unchanged run into a button that shows it", () => {
    const after = BEFORE.map((line, index) => (index === 1 || index === 27 ? `${line}!` : line));
    renderVi(<DocDiff lines={diffLines(BEFORE.join("\n"), after.join("\n"))} />);
    const region = screen.getByRole("region", { name: doc.diffLabel });
    // Changes at lines 2 and 28: context up to line 5 and from line 25, so lines 6..24 (19) are folded.
    expect(region.querySelectorAll("[data-kind]")).toHaveLength(32 - 19);
    const fold = screen.getByRole("button", { name: "⋯ 19 dòng không đổi" });
    fireEvent.click(fold);
    expect(screen.queryByTestId("doc-diff-fold")).toBeNull();
    expect(region.querySelectorAll("[data-kind]")).toHaveLength(32);
  });

  it("keeps a short unchanged run shown", () => {
    const after = BEFORE.slice(0, 14).map((line, index) => (index === 0 || index === 13 ? `${line}!` : line));
    renderVi(<DocDiff lines={diffLines(BEFORE.slice(0, 14).join("\n"), after.join("\n"))} />);
    expect(screen.queryByTestId("doc-diff-fold")).toBeNull();
  });

  it("shows the legend of the three kinds", () => {
    renderVi(<DocDiffLegend />);
    const items = screen.getByTestId("doc-diff-legend").querySelectorAll("li");
    expect(Array.from(items, (item) => item.dataset.kind)).toEqual(["add", "del", "same"]);
    expect(items[0]).toHaveTextContent(`+${doc.legendAdded}`);
    expect(items[1]).toHaveTextContent(`−${doc.legendRemoved}`);
    expect(items[2]).toHaveTextContent(doc.legendSame);
  });
});
