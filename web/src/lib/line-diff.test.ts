import { describe, expect, it } from "vitest";

import { type DiffLine, diffLines, diffStats } from "./line-diff";

const shape = (lines: DiffLine[]) => lines.map((line) => `${line.kind === "same" ? " " : line.kind === "add" ? "+" : "-"}${line.text}`);

describe("diffLines", () => {
  it("shows identical texts as unchanged lines numbered on both sides", () => {
    const lines = diffLines("a\nb\nc\n", "a\nb\nc\n");
    expect(shape(lines)).toEqual([" a", " b", " c"]);
    expect(lines.map((line) => [line.oldNo, line.newNo])).toEqual([[1, 1], [2, 2], [3, 3]]);
    expect(diffStats(lines)).toEqual({ added: 0, removed: 0 });
    expect(diffLines("", "")).toEqual([]);
  });

  it("shows added lines with only a new line number", () => {
    const lines = diffLines("a\nc", "a\nb\nc\nd");
    expect(shape(lines)).toEqual([" a", "+b", " c", "+d"]);
    expect(lines[1]).toEqual({ kind: "add", text: "b", oldNo: null, newNo: 2 });
    expect(lines[2]).toEqual({ kind: "same", text: "c", oldNo: 2, newNo: 3 });
    expect(diffStats(lines)).toEqual({ added: 2, removed: 0 });
    expect(shape(diffLines("", "x\ny\n"))).toEqual(["+x", "+y"]);
  });

  it("shows removed lines with only an old line number", () => {
    const lines = diffLines("a\nb\nc", "a\nc");
    expect(shape(lines)).toEqual([" a", "-b", " c"]);
    expect(lines[1]).toEqual({ kind: "del", text: "b", oldNo: 2, newNo: null });
    expect(lines[2]).toEqual({ kind: "same", text: "c", oldNo: 3, newNo: 2 });
    expect(shape(diffLines("x\ny", ""))).toEqual(["-x", "-y"]);
  });

  it("shows a replaced block as its removed lines before its added ones", () => {
    const lines = diffLines("a\nb\nc\nd", "a\nB\nC\nd");
    expect(shape(lines)).toEqual([" a", "-b", "-c", "+B", "+C", " d"]);
    expect(lines.map((line) => [line.oldNo, line.newNo])).toEqual([[1, 1], [2, null], [3, null], [null, 2], [null, 3], [4, 4]]);
    expect(diffStats(lines)).toEqual({ added: 2, removed: 2 });
  });

  it("treats \\r\\n as \\n", () => {
    expect(shape(diffLines("a\r\nb\r\n", "a\nb\n"))).toEqual([" a", " b"]);
    expect(shape(diffLines("a\r\nb", "a\r\nc"))).toEqual([" a", "-b", "+c"]);
  });

  it("adds no empty line for a final line end", () => {
    expect(shape(diffLines("a\nb\n", "a\nb"))).toEqual([" a", " b"]);
    expect(shape(diffLines("a\n", "a\n\n"))).toEqual([" a", "+"]);
  });

  it("diffs 5000-line files quickly, and a huge rewrite as removed then added", () => {
    const before = Array.from({ length: 5000 }, (_, index) => `line ${index}`);
    const after = before.map((line, index) => (index % 50 === 0 ? `${line} changed` : line));
    const started = performance.now();
    const lines = diffLines(before.join("\n"), after.join("\n"));
    expect(performance.now() - started).toBeLessThan(2000);
    expect(diffStats(lines)).toEqual({ added: 100, removed: 100 });

    const other = before.map((line) => `${line}!`);
    const rewrite = diffLines(before.join("\n"), other.join("\n"));
    expect(diffStats(rewrite)).toEqual({ added: 5000, removed: 5000 });
    expect(rewrite[0].kind).toBe("del");
    expect(rewrite[5000].kind).toBe("add");
  });
});
