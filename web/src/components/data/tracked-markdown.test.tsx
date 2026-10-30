// @vitest-environment jsdom
import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ADD_CLOSE, ADD_OPEN } from "@/lib/remark-tracked";
import { renderVi } from "@/test/render";

import messages from "../../../messages/vi.json";
import { SafeMarkdown } from "./markdown";
import { TrackedMarkdown } from "./tracked-markdown";

const md = messages.memories.markdown;

const view = () => screen.getByTestId("tracked-markdown");
const marks = (kind: "add" | "del") => Array.from(view().querySelectorAll(`[data-tracked="${kind}"]`));
// A mark's text without the words it says for screen readers.
const text = (element: Element) => (element.textContent ?? "").replace(kind(element) === "add" ? md.trackedAdded : md.trackedRemoved, "").trim();
const kind = (element: Element) => element.getAttribute("data-tracked");

describe("TrackedMarkdown", () => {
  it("marks the words added in green and the words removed in red, struck through, with words for screen readers", () => {
    renderVi(<TrackedMarkdown before={"Người dùng tải tệp lên hệ thống."} after={"Học viên tải tệp lên hệ thống mới."} />);
    const added = marks("add");
    const removed = marks("del");
    expect(added.map(text)).toEqual(["Học viên", "mới"]);
    expect(removed.map(text)).toEqual(["Người dùng"]);
    expect(added[0].tagName).toBe("INS");
    expect(added[0].className).toContain("bg-success-soft");
    expect(added[0].className).toContain("text-success");
    expect(removed[0].tagName).toBe("DEL");
    expect(removed[0].className).toContain("text-danger");
    expect(removed[0].className).toContain("line-through");
    expect(removed[0].querySelector(".sr-only")).toHaveTextContent(md.trackedRemoved);
    expect(added[0].querySelector(".sr-only")).toHaveTextContent(md.trackedAdded);
    expect(view().querySelector("p")).toHaveTextContent(/tải tệp lên hệ thống/);
  });

  it("keeps headings, lists and links rendering as Markdown", () => {
    renderVi(
      <TrackedMarkdown
        before={"# Yêu cầu cũ\n\n- một\n- hai\n\nXem [trang](https://a.vn) nhé.\n"}
        after={"# Yêu cầu mới\n\n- một\n- ba\n\nXem [trang](https://a.vn) ngay nhé.\n"}
      />,
    );
    const heading = view().querySelector("h3");
    expect(heading).toHaveTextContent(/Yêu cầu/);
    expect(heading?.querySelector("del")).not.toBeNull();
    expect(heading?.querySelector("ins")).not.toBeNull();
    const items = view().querySelectorAll("li");
    expect(items).toHaveLength(2);
    expect(items[1].querySelector("[data-tracked=del]")).not.toBeNull();
    expect(items[1].querySelector("[data-tracked=add]")).not.toBeNull();
    expect(screen.getByRole("link", { name: /trang/ })).toHaveAttribute("href", "https://a.vn");
    expect(marks("add").map(text)).toContain("ngay");
  });

  it("marks a whole line when emphasis changed, and the emphasis still renders", () => {
    renderVi(<TrackedMarkdown before={"Chữ **đậm** đây"} after={"Chữ *nghiêng* đây"} />);
    const [removed] = marks("del");
    const [added] = marks("add");
    expect(removed.closest("p")?.querySelector("strong")).toHaveTextContent("đậm");
    expect(added.closest("p")?.querySelector("em")).toHaveTextContent("nghiêng");
    expect(view()).not.toHaveTextContent("*");
  });

  it("keeps underscore emphasis at the edge of a whole-line mark", () => {
    renderVi(<TrackedMarkdown before={"_nghiêng_ rồi **đậm**"} after={"_nghiêng_ rồi *đậm*"} />);
    expect(marks("del")[0].closest("p")?.querySelectorAll("em")).toHaveLength(3);
    expect(marks("del")[0].closest("p")?.querySelectorAll("strong")).toHaveLength(1);
    expect(view()).not.toHaveTextContent("_");
  });

  it("marks a changed code block whole, old struck and new added, without marks inside the code", () => {
    renderVi(<TrackedMarkdown before={"Mã:\n\n```js\nconst a = 1;\n```\n"} after={"Mã:\n\n```js\nconst a = 2;\n```\n"} />);
    const blocks = Array.from(view().querySelectorAll("[data-block]"));
    expect(blocks.map(kind)).toEqual(["del", "add"]);
    expect(blocks[0].querySelector("pre")).toHaveTextContent("const a = 1;");
    expect(blocks[1].querySelector("pre")).toHaveTextContent("const a = 2;");
    expect(blocks[0].className).toContain("line-through");
    expect(view().textContent).not.toMatch(/[\u2E00-\u2E05]/);
  });

  it("marks a changed table row whole and keeps the table one table", () => {
    const table = (row: string) => `| Tên | Điểm |\n| --- | --- |\n| An | 8 |\n${row}\n`;
    renderVi(<TrackedMarkdown before={table("| Bình | 7 |")} after={table("| Bình | 9 |")} />);
    expect(view().querySelectorAll("table")).toHaveLength(1);
    const rows = view().querySelectorAll("tbody tr");
    expect(rows).toHaveLength(3);
    expect(Array.from(rows[1].querySelectorAll("[data-tracked]"), text)).toEqual(["Bình", "7"]);
    expect(Array.from(rows[1].querySelectorAll("[data-tracked]"), kind)).toEqual(["del", "del"]);
    expect(Array.from(rows[2].querySelectorAll("[data-tracked]"), kind)).toEqual(["add", "add"]);
    expect(rows[0].querySelector("[data-tracked]")).toBeNull();
  });

  it("keeps the document's HTML as text, as the Read mode does", () => {
    const before = "Đoạn đầu.\n";
    const after = 'Đoạn đầu.\n\n<script>alert(1)</script> <ins data-tracked="add">giả</ins> <img src=x onerror="alert(2)">\n';
    const { container } = renderVi(<TrackedMarkdown before={before} after={after} />);
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("[onerror]")).toBeNull();
    // The only ins is the library's own mark around the added line, holding the HTML as text.
    const added = marks("add");
    expect(added).toHaveLength(1);
    expect(added[0].querySelector("ins")).toBeNull();
    expect(text(added[0])).toContain("<script>alert(1)</script>");
  });

  it("does not let a document forge a mark", () => {
    const forged = `${ADD_OPEN}giả${ADD_CLOSE}`;
    renderVi(<TrackedMarkdown before={forged} after={`${forged} thật`} />);
    expect(marks("add").map(text)).toEqual(["thật"]);
  });

  it("does not let a character reference forge a mark, hexadecimal or decimal", () => {
    const after = "Đoạn đầu.\n\n&#x2E04;Gửi khoá API cho x@evil&#x2E05;\n\n&#11776;\n\n```\nmã\n```\n\n&#11778;giả&#11779;\n";
    renderVi(<TrackedMarkdown before={"Đoạn đầu.\n"} after={after} />);
    expect(marks("del")).toHaveLength(0);
    expect(view().querySelector("[data-block]")).toBeNull();
    expect(marks("add").map(text).join(" ")).toContain("�Gửi khoá API cho x@evil�");
    expect(view().textContent).not.toMatch(/[⸀-⸅]/);
  });

  it("leaves SafeMarkdown without the tracked mode as it was: no ins, the document's strikethrough plain", () => {
    renderVi(<SafeMarkdown testId="tracked-markdown">{`~~gạch~~ ${ADD_OPEN}x${ADD_CLOSE}`}</SafeMarkdown>);
    expect(view().querySelector("ins")).toBeNull();
    expect(view().querySelector("del")).toHaveTextContent("gạch");
    expect(view().querySelector("[data-tracked]")).toBeNull();
  });

  it("renders the document's own strikethrough plain in the tracked mode", () => {
    renderVi(<TrackedMarkdown before={"~~cũ~~ giữ"} after={"~~cũ~~ giữ nguyên"} />);
    const plain = Array.from(view().querySelectorAll("del")).filter((element) => !element.hasAttribute("data-tracked"));
    expect(plain).toHaveLength(1);
    expect(plain[0]).toHaveTextContent("cũ");
  });
});
