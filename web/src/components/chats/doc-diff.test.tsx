// @vitest-environment jsdom
import { fireEvent, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { DocDiff, DocDiffLegend } from "@/components/data/doc-diff";
import { diffLines } from "@/lib/line-diff";
import { renderVi } from "@/test/render";

import messages from "../../../messages/vi.json";

const doc = messages.chats.doc;

const BEFORE = Array.from({ length: 30 }, (_, index) => `line ${index + 1}`);

describe("DocDiff", () => {
  it("signs added and removed rows and marks each row's kind", () => {
    renderVi(<DocDiff lines={diffLines("a\nb\nc", "a\nB\nc\nd")} />);
    const rows = screen.getByTestId("doc-diff").querySelectorAll("[data-kind]");
    expect(Array.from(rows, (row) => row.getAttribute("data-kind"))).toEqual(["same", "del", "add", "same", "add"]);
    expect(rows[1]).toHaveTextContent(`2−${doc.removedLine} b`);
    expect(rows[2]).toHaveTextContent(`2+${doc.addedLine} B`);
    expect(rows[1].className).toContain("bg-danger-soft");
    expect(rows[2].className).toContain("bg-success-soft");
    expect(rows[0].className).not.toContain("bg-");
    expect(rows[0]).toHaveTextContent(/^11 a$/);
  });

  it("strikes a removed line through in red and leaves an added one plain on green", () => {
    renderVi(<DocDiff lines={diffLines("giữ\nbỏ hẳn dòng này", "giữ\nthêm")} />);
    const removed = screen.getByTestId("doc-diff").querySelector("[data-kind=del] > span:last-child");
    expect(removed?.className).toContain("line-through");
    expect(removed?.className).toContain("text-danger");
    expect(removed?.querySelector("[data-word]")).toBeNull();
  });

  it("tints the words that differ in a changed line, Vietnamese words whole", () => {
    renderVi(<DocDiff lines={diffLines("Người dùng tải tệp lên.", "Học viên tải tệp lên.")} />);
    const rows = screen.getByTestId("doc-diff").querySelectorAll("[data-kind]");
    const words = (row: Element) => Array.from(row.querySelectorAll("[data-word]"), (word) => [word.getAttribute("data-word"), word.textContent]);
    expect(words(rows[0])).toEqual([["del", "Người dùng"]]);
    expect(words(rows[1])).toEqual([["add", "Học viên"]]);
    expect(rows[0]).toHaveTextContent(`1−${doc.removedLine} Người dùng tải tệp lên.`);
    expect(rows[1]).toHaveTextContent(`1+${doc.addedLine} Học viên tải tệp lên.`);
    expect(rows[0].querySelector("[data-word=del]")?.className).toContain("bg-danger-solid");
    expect(rows[1].querySelector("[data-word=add]")?.className).toContain("bg-success-solid");
  });

  it("does not tint words of lines that share too little", () => {
    renderVi(<DocDiff lines={diffLines("alpha beta", "gamma delta")} />);
    expect(screen.getByTestId("doc-diff").querySelector("[data-word]")).toBeNull();
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

  it("says what the marked view's colours mean: added on green, removed in red and struck through, unchanged", () => {
    renderVi(<DocDiffLegend tracked />);
    const legend = screen.getByTestId("doc-diff-legend");
    expect(legend).toHaveAttribute("data-view", "tracked");
    const items = legend.querySelectorAll("li");
    expect(Array.from(items, (item) => item.textContent)).toEqual([`Aa${doc.legendAdded}`, `Aa${doc.legendRemoved}`, `Aa${doc.legendSame}`]);
    expect(doc.legendAdded).toBe("Đã thêm");
    expect(doc.legendRemoved).toBe("Đã xoá");
    expect(doc.legendSame).toBe("Giữ nguyên");
    expect(items[0].querySelector("span")!.className).toContain("bg-success-soft");
    expect(items[1].querySelector("span")!.className).toContain("line-through");
    expect(items[1].querySelector("span")!.className).toContain("text-danger");
  });
});
