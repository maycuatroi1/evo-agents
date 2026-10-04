// @vitest-environment jsdom
import { screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { renderVi } from "@/test/render";

import { SafeMarkdown } from "./markdown";

declare global {
  interface Window {
    __pwned?: boolean;
  }
}

afterEach(() => {
  delete window.__pwned;
});

function rendered(markdown: string): HTMLElement {
  renderVi(<SafeMarkdown>{markdown}</SafeMarkdown>);
  return screen.getByTestId("memory-markdown");
}

describe("SafeMarkdown", () => {
  it("shows HTML in a memory as text and never runs it", () => {
    const root = rendered(
      [
        "Before <script>window.__pwned = true</script> after",
        "",
        "<img src=x onerror=\"window.__pwned = true\">",
        "",
        "<iframe src=\"https://evil.example\"></iframe>",
        "",
        "<div onclick=\"window.__pwned = true\">click</div>",
      ].join("\n"),
    );
    expect(root.querySelector("script, img, iframe, div[onclick], [onerror]")).toBeNull();
    expect(root.textContent).toContain("<script>window.__pwned = true</script>");
    expect(root.textContent).toContain('<img src=x onerror="window.__pwned = true">');
    expect(window.__pwned).toBeUndefined();
    for (const element of root.querySelectorAll("*")) {
      for (const attribute of element.getAttributeNames()) expect(attribute.startsWith("on"), attribute).toBe(false);
    }
  });

  it("keeps http(s), mailto and in-page links and makes every other target inert", () => {
    const root = rendered(
      [
        "[site](https://example.org/a) [mail](mailto:a@example.org) [note](#part)",
        "[js](javascript:window.__pwned=true) [data](data:text/html,hi) [vb](vbscript:x) [file](other.md)",
      ].join("\n\n"),
    );
    const hrefs = [...root.querySelectorAll("a")].map((a) => a.getAttribute("href"));
    expect(hrefs).toEqual(["https://example.org/a", "mailto:a@example.org", "#part"]);
    const external = screen.getByRole("link", { name: /site/ });
    expect(external).toHaveAttribute("rel", "noopener noreferrer nofollow");
    expect(external).toHaveAttribute("target", "_blank");
    for (const inert of ["js", "data", "vb", "file"]) {
      const text = screen.getByText(inert);
      expect(text.tagName).toBe("SPAN");
      expect(text.closest("a")).toBeNull();
    }
  });

  it("never loads an image: it becomes a link to it, or a placeholder", () => {
    const root = rendered("![a diagram](https://images.example/d.png)\n\n![](relative.png)");
    expect(root.querySelector("img")).toBeNull();
    expect(screen.getByRole("link", { name: /Hình: a diagram/ })).toHaveAttribute("href", "https://images.example/d.png");
    expect(screen.getByText("Hình không có mô tả").closest("a")).toBeNull();
  });

  it("renders GitHub Markdown under the page's headings", () => {
    const root = rendered(
      [
        "# Title",
        "## Section",
        "- [x] done",
        "- [ ] open",
        "",
        "| a | b |",
        "| - | - |",
        "| 1 | 2 |",
        "",
        "~~gone~~ and `code`",
        "",
        "```",
        "block",
        "```",
      ].join("\n"),
    );
    expect(root.querySelector("h1, h2")).toBeNull();
    expect(screen.getByRole("heading", { level: 3, name: "Title" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 4, name: "Section" })).toBeInTheDocument();
    expect(root.querySelector("input")).toBeNull(); // task boxes are icons with words
    expect(screen.getByText("Đã xong:")).toBeInTheDocument();
    expect(screen.getByText("Chưa xong:")).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Bảng" }).querySelector("table")).not.toBeNull();
    expect(root.querySelector("del")?.textContent).toBe("gone");
    expect(root.querySelector("pre")).toHaveAttribute("tabindex", "0");
  });
});
