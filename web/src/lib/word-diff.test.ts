import { describe, expect, it } from "vitest";

import { diffLines } from "./line-diff";
import { markedMarkdown } from "./marked-markdown";
import { ADD_CLOSE, ADD_OPEN, BLOCK_ADD, BLOCK_DEL, DEL_CLOSE, DEL_OPEN } from "./remark-tracked";
import { diffWords, lineWords, newBudget, sharedShare, tokenize, type WordPart } from "./word-diff";

const shape = (parts: WordPart[] | null) => parts?.map((part) => `${part.kind === "same" ? "=" : part.kind === "add" ? "+" : "-"}${part.text}`);
// The marks as readable brackets: {+added+} and [-removed-], a block as <+> or <->.
const readable = (text: string) =>
  text
    .replaceAll(ADD_OPEN, "{+")
    .replaceAll(ADD_CLOSE, "+}")
    .replaceAll(DEL_OPEN, "[-")
    .replaceAll(DEL_CLOSE, "-]")
    .replaceAll(BLOCK_ADD, "<+>")
    .replaceAll(BLOCK_DEL, "<->");
const marked = (before: string, after: string) => readable(markedMarkdown(before, after));

describe("tokenize", () => {
  it("keeps a Vietnamese word whole, precomposed or decomposed, and cuts spaces and punctuation apart", () => {
    expect(tokenize("Tiếng Việt có dấu, rất đẹp!")).toEqual(["Tiếng", " ", "Việt", " ", "có", " ", "dấu", ",", " ", "rất", " ", "đẹp", "!"]);
    const decomposed = "Nguyễn Thị Hướng".normalize("NFD");
    expect(tokenize(decomposed)).toEqual(decomposed.split(/(\s+)/));
    expect(tokenize("**đậm**_x_")).toEqual(["*", "*", "đậm", "*", "*", "_", "x", "_"]);
    expect(tokenize("")).toEqual([]);
  });
});

describe("diffWords", () => {
  it("marks the changed words of a Vietnamese sentence, never a part of a word", () => {
    const parts = diffWords("Hệ thống cho phép người dùng tải tệp lên.", "Hệ thống cho phép học viên tải tệp lên nhanh.");
    expect(shape(parts)).toEqual(["=Hệ thống cho phép ", "-người dùng", "+học viên", "= tải tệp lên", "+ nhanh", "=."]);
  });

  it("joins the old and new text back from its parts", () => {
    const before = "một hai ba bốn năm sáu";
    const after = "một HAI ba năm bảy sáu tám";
    const parts = diffWords(before, after) ?? [];
    expect(parts.filter((part) => part.kind !== "add").map((part) => part.text).join("")).toBe(before);
    expect(parts.filter((part) => part.kind !== "del").map((part) => part.text).join("")).toBe(after);
  });

  it("reads a run of replaced words as one replacement, removed before added", () => {
    expect(shape(diffWords("x a b c y", "x d e f y"))).toEqual(["=x ", "-a b c", "+d e f", "= y"]);
  });

  it("measures the share of text kept", () => {
    expect(sharedShare(diffWords("abc", "abc") ?? [])).toBe(1);
    expect(sharedShare(diffWords("abc", "xyz") ?? [])).toBe(0);
    expect(sharedShare([])).toBe(1);
  });

  it("gives up when the search is over budget", () => {
    const words = (seed: number) => Array.from({ length: 3000 }, (_, index) => `w${(index * seed) % 997}`).join(" ");
    expect(diffWords(words(7), words(13), newBudget(1000))).toBeNull();
    const budget = newBudget(100);
    expect(diffWords("a b", "a c", budget)).not.toBeNull();
    expect(budget.left).toBeLessThan(100);
  });
});

describe("lineWords", () => {
  it("pairs each removed line with the added line in its place and keeps each side's words", () => {
    const lines = diffLines("tiêu đề\ncâu cũ ở đây\nđuôi", "tiêu đề\ncâu mới ở đây\nđuôi\nthêm");
    const words = lineWords(lines);
    expect(lines.map((line) => line.kind)).toEqual(["same", "del", "add", "same", "add"]);
    expect(shape(words[1])).toEqual(["=câu ", "-cũ", "= ở đây"]);
    expect(shape(words[2])).toEqual(["=câu ", "+mới", "= ở đây"]);
    expect(words[0]).toBeNull();
    expect(words[4]).toBeNull();
  });

  it("marks a line whole when it shares too little with its pair", () => {
    const words = lineWords(diffLines("alpha beta gamma", "zeta eta theta"));
    expect(words).toEqual([null, null]);
  });
});

describe("markedMarkdown", () => {
  it("marks changed words in a paragraph and keeps the rest as it was", () => {
    expect(marked("# Yêu cầu\n\nNgười dùng tải tệp lên.\n", "# Yêu cầu\n\nHọc viên tải tệp lên.\n")).toBe(
      "# Yêu cầu\n\n[-Người dùng-]{+Học viên+} tải tệp lên.",
    );
  });

  it("keeps a heading or a list item's syntax outside the marks", () => {
    expect(marked("## Mục cũ\n- một\n- hai", "## Mục mới\n- một\n- ba\n- bốn")).toBe(
      "## Mục [-cũ-]{+mới+}\n- một\n- [-hai-] {+ba+}\n- {+bốn+}",
    );
    expect(marked("- việc\n", "1. việc\n")).toBe("- [-việc-]\n1. {+việc+}");
    expect(marked("a\n# Bỏ\nb", "a\nb")).toBe("a\n# [-Bỏ-]\nb");
  });

  it("marks a line whole rather than put a mark inside emphasis, code or a link", () => {
    expect(marked("Xem **cũ** nhé", "Xem *cũ* nhé")).toBe("[-Xem **cũ** nhé-] {+Xem *cũ* nhé+}");
    expect(marked("Gọi `foo bar` đi", "Gọi `foo baz` đi")).toBe("[-Gọi `foo bar` đi-] {+Gọi `foo baz` đi+}");
    expect(marked("[trang](https://a.vn/x) đây", "[trang](https://a.vn/y) đây")).toBe(
      "[-[trang](https://a.vn/x) đây-] {+[trang](https://a.vn/y) đây+}",
    );
    // A change beside emphasis but outside it is marked word by word.
    expect(marked("Có **đậm** và cũ", "Có **đậm** và mới")).toBe("Có **đậm** và [-cũ-]{+mới+}");
  });

  it("marks a changed fenced code block whole, old then new, and leaves an unchanged one alone", () => {
    const before = "Mở đầu\n\n```js\nconst a = 1;\n```\n\n```\nkhông đổi\n```\n";
    const after = "Mở đầu\n\n```js\nconst a = 2;\n```\n\n```\nkhông đổi\n```\n";
    expect(marked(before, after)).toBe(
      "Mở đầu\n\n<->\n\n```js\nconst a = 1;\n```\n\n<+>\n\n```js\nconst a = 2;\n```\n\n```\nkhông đổi\n```",
    );
  });

  it("marks a table's changed rows whole, cell by cell, and a table whose header changed as a block", () => {
    const table = (rows: string[], header = "| Tên | Số |") => [header, "| --- | --- |", ...rows].join("\n");
    expect(marked(table(["| a | 1 |", "| b | 2 |"]), table(["| a | 1 |", "| b | 3 |"]))).toBe(
      table(["| a | 1 |", "| [-b-] | [-2-] |", "| {+b+} | {+3+} |"]),
    );
    expect(marked(table(["| a | 1 |"]), table(["| a | 1 |"], "| Tên | Tuổi |"))).toBe(
      `<->\n\n${table(["| a | 1 |"])}\n\n<+>\n\n${table(["| a | 1 |"], "| Tên | Tuổi |")}\n`,
    );
  });

  it("replaces mark characters the document holds, so it cannot forge a mark", () => {
    const forged = `${ADD_OPEN}giả${ADD_CLOSE} và ${BLOCK_DEL}`;
    expect(markedMarkdown(forged, forged)).toBe("\uFFFDgiả\uFFFD và \uFFFD");
  });

  it("leaves a removed link definition out and keeps an added one as it is", () => {
    expect(marked("[a]: https://a.vn\nchữ", "[b]: https://b.vn\nchữ")).toBe("[b]: https://b.vn\nchữ");
  });

  it("marks a document of 2 MiB quickly", () => {
    const line = (index: number) => `Dòng số ${index}: nội dung tiếng Việt có dấu để kiểm tra tốc độ đánh dấu.`;
    const before = Array.from({ length: 28_000 }, (_, index) => line(index)).join("\n");
    expect(new TextEncoder().encode(before).length).toBeGreaterThan(2 * 1024 * 1024);
    const after = before
      .split("\n")
      .map((text, index) => (index % 50 === 0 ? text.replace("nội dung", "phần thân") : text))
      .join("\n");
    const started = performance.now();
    const out = markedMarkdown(before, after);
    expect(performance.now() - started).toBeLessThan(5000);
    expect(readable(out)).toContain("Dòng số 50: [-nội dung-]{+phần thân+} tiếng Việt");
    // A rewrite of every line spends the shared budget and still ends.
    const rewritten = before.split("\n").map((text) => text.split("").reverse().join("")).join("\n");
    const again = performance.now();
    markedMarkdown(before, rewritten);
    expect(performance.now() - again).toBeLessThan(10000);
  });
});
