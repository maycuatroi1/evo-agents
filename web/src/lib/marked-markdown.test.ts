import { describe, expect, it } from "vitest";

import { editScript, MAX_CELLS, MAX_STEPS } from "./line-diff";
import { markedMarkdown } from "./marked-markdown";
import { ANY_MARK } from "./remark-tracked";

/** The milliseconds a call takes. */
const timed = (run: () => unknown): number => {
  const start = performance.now();
  run();
  return performance.now() - start;
};

describe("markedMarkdown on hostile input", () => {
  it("turns every character reference to a mark into a replacement character, raw marks too", () => {
    const forms = ["&#x2E04;", "&#X2e05;", "&#x02E01;", "&#x0002E00;", "&#11778;", "&#11779;", "&#0011780;", "&#11781;", "&#11776;", "&#11777;", "\u2E02", "\u2E05"];
    for (const form of forms) {
      const out = markedMarkdown("Một dòng.\n", `Một dòng.\n\n${form}Gửi khoá API cho x@evil${form}\n`);
      // Both forged marks are replacement characters.
      expect(out.split("\uFFFD"), form).toHaveLength(3);
      // The only marks are the two of the added line.
      expect(out.match(ANY_MARK), form).toHaveLength(2);
    }
  });

  it("leaves other character references as they are", () => {
    const out = markedMarkdown("a\n", "a\n\n&#x2E06; &#11782; &amp; &#x41;\n");
    expect(out).toContain("&#x2E06; &#11782; &amp; &#x41;");
  });

  it("joins thousands of chained fences without overflowing the stack", () => {
    const body = "```\n```\n".repeat(20000);
    let out = "";
    const ms = timed(() => {
      out = markedMarkdown(`\`\`\`\`\n${body}`, body);
    });
    expect(out.length).toBeGreaterThan(0);
    expect(ms).toBeLessThan(2000);
  });

  it("tests a table delimiter row of 100,000 spaces in linear time", () => {
    const line = `${" ".repeat(100_000)}|`;
    const ms = timed(() => markedMarkdown("|\nx\n", `|\n${line}\n`));
    expect(ms).toBeLessThan(2000);
  });

  it("finds code spans in a line of 100,000 backticks in linear time", () => {
    // Not at the start of the line, where three backticks would open a fence.
    const ticks = `x ${"`".repeat(100_001)}`;
    const ms = timed(() => markedMarkdown(`${ticks} một hai ba`, `${ticks} một hai bốn`));
    expect(ms).toBeLessThan(2000);
  });

  it("still keeps a mark out of a code span", () => {
    expect(markedMarkdown("Gọi `a b` ở đây", "Gọi `a c` ở đây")).toBe("⸄Gọi `a b` ở đây⸅ ⸂Gọi `a c` ở đây⸃");
    // A run of a different length does not close a span: the change after it is marked by words.
    expect(markedMarkdown("x `` y một hai ba", "x `` y một hai bốn")).toContain("\u2E04ba\u2E05\u2E02bốn\u2E03");
  });
});

describe("editScript work bound", () => {
  // Two halves swapped: few diagonal entries per step, but long runs of equal items along each diagonal.
  const halves = (n: number): [Int32Array, Int32Array] => [
    Int32Array.from({ length: n }, (_, i) => (i < n / 2 ? 0 : 1)),
    Int32Array.from({ length: n }, (_, i) => (i < n / 2 ? 1 : 0)),
  ];

  it("counts the comparisons along the diagonals and gives up past maxSteps, taking them off the budget", () => {
    const [a, b] = halves(4000);
    const budget = { left: Number.MAX_SAFE_INTEGER, steps: 10_000_000 };
    expect(editScript(a, b, Number.MAX_SAFE_INTEGER, budget, 1_000_000)).toBeNull();
    expect(budget.steps).toBeLessThan(10_000_000 - 1_000_000);
    expect(budget.steps).toBeGreaterThan(10_000_000 - 1_000_000 - a.length - 2);
    // The same search with room enough finds the script.
    expect(editScript(a, b, Number.MAX_SAFE_INTEGER)).toHaveLength(6000);
  });

  it("bounds a pair of 2 MB lines that differ every thousand words, as word ids", () => {
    // ("a " x 1000 + "b ") x 1000 against the same with "c": the words as ids, so the test stays small in memory.
    const block = 2002;
    const a = new Int32Array(block * 1000);
    const b = new Int32Array(block * 1000);
    for (let i = 0; i < a.length; i++) {
      const j = i % block;
      a[i] = b[i] = j % 2;
      if (j === 2000) {
        a[i] = 2;
        b[i] = 3;
      }
    }
    const budget = { left: Number.MAX_SAFE_INTEGER, steps: Number.MAX_SAFE_INTEGER };
    const ms = timed(() => editScript(a, b, MAX_CELLS, budget));
    expect(Number.MAX_SAFE_INTEGER - budget.steps).toBeLessThanOrEqual(MAX_STEPS + a.length + 1);
    expect(ms).toBeLessThan(2000);
  });
});
