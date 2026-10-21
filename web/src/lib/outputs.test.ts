import { describe, expect, it } from "vitest";

import { parseCsv, prettyJson } from "./output-text";
import { baseName, byteParts, downloadHref, extensionOf, outputLinks, textKind } from "./outputs";

const SHA = "a".repeat(64);
const OTHER = "b".repeat(64);

describe("outputLinks", () => {
  it("reads the resource_link of an image taken out of a tool's result, in a tool call content or a list", () => {
    const content = [
      { type: "content", content: { type: "text", text: "screenshot taken" } },
      { type: "content", content: { type: "resource_link", uri: `evo-output:sha256:${SHA}`, name: "shot.png", mimeType: "image/png", size: 1200 } },
      { type: "resource_link", uri: `evo-output:sha256:${OTHER}`, name: "two.jpg" },
      { type: "content", content: { type: "resource_link", uri: `evo-output:sha256:${SHA}`, name: "again.png" } },
    ];
    expect(outputLinks(content)).toEqual([
      { sha256: SHA, name: "shot.png", mediaType: "image/png", size: 1200 },
      { sha256: OTHER, name: "two.jpg", mediaType: null, size: null },
    ]);
  });

  it("passes over links of other schemes, shapes it does not know and text", () => {
    expect(outputLinks([{ type: "resource_link", uri: "https://example.org/a.png" }, { type: "resource_link", uri: "evo-output:sha256:short" }, "text", null, 7])).toEqual([]);
    expect(outputLinks({ type: "output", source: "tool", sha256: SHA })).toEqual([]);
  });
});

describe("names and kinds", () => {
  it("names a file of the output folder by its last part, and its extension in lower case", () => {
    expect(baseName("shots/home/Page.PNG")).toBe("Page.PNG");
    expect(extensionOf("shots/home/Page.PNG")).toBe("png");
    expect(extensionOf(".env")).toBe("");
    expect(extensionOf("Makefile")).toBe("");
  });

  it("reads text as Markdown, JSON, CSV, code or plain text", () => {
    expect(textKind({ name: "report.md", media_type: "text/markdown" })).toBe("markdown");
    expect(textKind({ name: "events.jsonl", media_type: "application/x-ndjson" })).toBe("json");
    expect(textKind({ name: "rows.tsv", media_type: "text/plain" })).toBe("csv");
    expect(textKind({ name: "main.py", media_type: "text/x-python" })).toBe("code");
    expect(textKind({ name: "page.tsx", media_type: "text/plain" })).toBe("code");
    expect(textKind({ name: "run.log", media_type: "text/plain" })).toBe("plain");
  });

  it("asks for a download with ?download=true, after any query the URL has", () => {
    expect(downloadHref("/v1/projects/p/runs/1/outputs/2/content")).toBe("/v1/projects/p/runs/1/outputs/2/content?download=true");
    expect(downloadHref("/x?a=1")).toBe("/x?a=1&download=true");
  });

  it("gives sizes in binary steps", () => {
    expect(byteParts(230)).toEqual({ value: 230, unit: "byte" });
    expect(byteParts(1536)).toEqual({ value: 1.5, unit: "kilobyte" });
    expect(byteParts(200 * 1024 * 1024)).toEqual({ value: 200, unit: "megabyte" });
  });
});

describe("prettyJson", () => {
  it("indents JSON and each value of JSON Lines, and keeps text that does not parse", () => {
    expect(prettyJson('{"a":[1,2]}')).toBe('{\n  "a": [\n    1,\n    2\n  ]\n}');
    expect(prettyJson('{"a":1}\n{"b":2}\n')).toBe('{\n  "a": 1\n}\n{\n  "b": 2\n}');
    expect(prettyJson("not json {")).toBe("not json {");
  });
});

describe("parseCsv", () => {
  it("reads quoted fields with delimiters, line breaks and doubled quotes", () => {
    const text = 'name,note\r\n"Lumen, Inc.","said ""hi""\nthen left"\nnorthwind,\n';
    expect(parseCsv(text).rows).toEqual([
      ["name", "note"],
      ["Lumen, Inc.", 'said "hi"\nthen left'],
      ["northwind", ""],
    ]);
  });

  it("reads TSV with a tab, and stops at the limit saying there is more", () => {
    expect(parseCsv("a\tb\n1\t2", "\t").rows).toEqual([["a", "b"], ["1", "2"]]);
    const many = Array.from({ length: 10 }, (_, index) => `row${index}`).join("\n");
    expect(parseCsv(many, ",", 4)).toEqual({ rows: [["row0"], ["row1"], ["row2"], ["row3"]], more: true });
    expect(parseCsv("x\ny", ",", 4).more).toBe(false);
  });
});
