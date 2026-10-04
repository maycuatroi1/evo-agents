import { describe, expect, it } from "vitest";

import { labelNames, memoryTitle, parseFrontmatter } from "./memory-meta";

describe("parseFrontmatter", () => {
  it("splits the frontmatter off and reads its plain top-level fields", () => {
    const text = [
      "---",
      "name: Deploy notes",
      'description: "How we ship: carefully"',
      "metadata:",
      "  type: project",
      "tags: >",
      "---",
      "# Body",
    ].join("\n");
    const parsed = parseFrontmatter(text);
    expect(parsed.fields).toEqual({ name: "Deploy notes", description: "How we ship: carefully" });
    expect(parsed.raw).toBe('name: Deploy notes\ndescription: "How we ship: carefully"\nmetadata:\n  type: project\ntags: >');
    expect(parsed.body).toBe("# Body");
  });

  it("leaves a file without frontmatter whole", () => {
    expect(parseFrontmatter("# Just text\n---\n")).toEqual({ raw: null, fields: {}, body: "# Just text\n---\n" });
    expect(parseFrontmatter("---\n---\nbody").raw).toBe("");
    expect(parseFrontmatter("---\r\nname: x\r\n---\r\nbody")).toMatchObject({ fields: { name: "x" }, body: "body" });
  });

  it("titles a memory by its frontmatter name, else its file name", () => {
    expect(memoryTitle("a.md", "---\nname: Alpha\ndescription: first\n---\n")).toEqual({ title: "Alpha", description: "first" });
    expect(memoryTitle("a.md", "no frontmatter")).toEqual({ title: "a.md", description: null });
  });
});

describe("labelNames", () => {
  it("reads names and nothing else", () => {
    expect(labelNames({ level: "internal", location: "any", integrity: "T", projects: ["x"] })).toEqual({
      level: "internal",
      location: "any",
      integrity: "T",
    });
    expect(labelNames({ level: 3, location: "" })).toEqual({ level: null, location: null, integrity: null });
  });
});
