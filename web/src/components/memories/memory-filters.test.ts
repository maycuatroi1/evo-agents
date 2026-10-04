import { describe, expect, it } from "vitest";

import { applyFilters, countBy, filtersQuery, locationOptions, readFilters, recordParams } from "./memory-filters";

const items = [
  { location: "harness", type: "project" },
  { location: "api", type: "reference" },
  { location: "api", type: "user" },
  { location: "old-repo", type: "project" },
];

describe("memory filters", () => {
  it("reads the URL strictly: an unknown type or an overlong location is no filter", () => {
    expect(readFilters(new URLSearchParams("q=%20deploy%20&location=api&type=feedback"))).toEqual({
      q: "deploy",
      location: "api",
      type: "feedback",
    });
    expect(readFilters(new URLSearchParams(`type=secret&location=${"x".repeat(256)}`))).toEqual({
      q: "",
      location: null,
      type: null,
    });
    expect(readFilters(new URLSearchParams(`q=${"a".repeat(600)}`)).q).toHaveLength(500);
    expect(readFilters(recordParams({ q: ["first", "second"], type: "user" }))).toEqual({ q: "first", location: null, type: "user" });
  });

  it("writes only what is set", () => {
    expect(filtersQuery({ q: "", location: null, type: null })).toBe("");
    expect(filtersQuery({ q: " a b ", location: "api", type: "user" })).toBe("?q=a+b&location=api&type=user");
  });

  it("narrows by location and type and counts each facet", () => {
    expect(applyFilters(items, { q: "", location: "api", type: null })).toHaveLength(2);
    expect(applyFilters(items, { q: "", location: "api", type: "user" })).toEqual([{ location: "api", type: "user" }]);
    expect(countBy(items, (item) => item.location)).toEqual(new Map([["harness", 1], ["api", 2], ["old-repo", 1]]));
  });

  it("offers the declared locations first, then any other a memory sits in", () => {
    expect(locationOptions(["harness", "api", "web"], items)).toEqual(["harness", "api", "web", "old-repo"]);
    expect(locationOptions([], items)).toEqual(["api", "harness", "old-repo"]);
  });
});
