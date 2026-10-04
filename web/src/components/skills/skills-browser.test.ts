import { describe, expect, it } from "vitest";

import { bundleFileName, shortHash, sizeParts } from "./format";
import { skillHref, skillsHref } from "./queries";
import { matchesSkill } from "./skills-browser";

describe("skills helpers", () => {
  it("filters on every word of the name and description, ignoring case", () => {
    const skill = { name: "team-notes", description: "Use when writing Release notes" };
    expect(matchesSkill(skill, "TEAM release")).toBe(true);
    expect(matchesSkill(skill, "team deploy")).toBe(false);
    expect(matchesSkill(skill, "   ")).toBe(true);
  });

  it("names sizes, hashes and files as the pages show them", () => {
    expect(sizeParts(512)).toEqual({ value: 512, unit: "byte" });
    expect(sizeParts(2048)).toEqual({ value: 2, unit: "kilobyte" });
    expect(sizeParts(3 * 1024 * 1024)).toEqual({ value: 3, unit: "megabyte" });
    expect(shortHash("abcdef0123456789")).toBe("abcdef012345");
    expect(bundleFileName("team-notes", 3)).toBe("team-notes-v3.tar.gz");
  });

  it("links a skill under its place", () => {
    expect(skillsHref({ kind: "global" })).toBe("/skills");
    expect(skillsHref({ kind: "project", project: "demo" })).toBe("/p/demo/skills");
    expect(skillHref({ kind: "project", project: "demo" }, "a.b")).toBe("/p/demo/skills/a.b");
    expect(skillHref({ kind: "global" }, "house-style")).toBe("/skills/house-style");
  });
});
