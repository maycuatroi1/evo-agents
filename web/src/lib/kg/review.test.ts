import { describe, expect, it } from "vitest";

import { kgKeys } from "./queries";
import { groupReview, nodeNames, parseReviewFilter, REVIEW_RULES } from "./review";
import { reviewHref } from "./routes";
import type { ReviewItem } from "./types";

const item = (fingerprint: string, rule: ReviewItem["rule"], cluster: string | null, weight: number, extra: Partial<ReviewItem> = {}): ReviewItem => ({
  fingerprint,
  rule,
  action: "link_or_deprecate",
  cluster,
  cluster_label: cluster ? `label ${cluster}` : null,
  nodes: [{ id: `doc:${fingerprint}`, kind: "Document", name: fingerprint }],
  detail: { count: 1 },
  weight,
  dismissal: null,
  ...extra,
});

describe("review queue helpers", () => {
  it("groups by rule in the hub's order, then by cluster most connected first, no cluster last", () => {
    const groups = groupReview([
      item("a", "bulk_noise", "c1", 1),
      item("b", "unmentioned_document", null, 9),
      item("c", "unmentioned_document", "c2", 2),
      item("d", "unmentioned_document", "c1", 5),
      item("e", "unmentioned_document", "c2", 7),
      item("f", "dangling_mention", "c1", 0),
    ]);
    expect(groups.map((g) => g.rule)).toEqual(["dangling_mention", "unmentioned_document", "bulk_noise"]);
    const documents = groups[1]!;
    expect(documents.items).toBe(4);
    expect(documents.clusters.map((c) => c.cluster)).toEqual(["c2", "c1", null]);
    expect(documents.clusters[0]!.items.map((i) => i.fingerprint)).toEqual(["e", "c"]);
    expect(documents.clusters[0]!.label).toBe("label c2");
    expect(groupReview([])).toEqual([]);
  });

  it("knows the six rules of the hub", () => {
    expect(REVIEW_RULES).toHaveLength(6);
    expect(new Set(REVIEW_RULES).size).toBe(6);
  });

  it("reads the filter from the URL and drops what the hub would refuse", () => {
    expect(parseReviewFilter({})).toEqual({ rule: null, cluster: null, state: "open" });
    expect(parseReviewFilter({ rule: "possible_duplicate", cluster: ["c3", "c4"], state: "all" })).toEqual({
      rule: "possible_duplicate",
      cluster: "c3",
      state: "all",
    });
    expect(parseReviewFilter({ rule: "nonsense", cluster: "x".repeat(201), state: "closed" })).toEqual({
      rule: null,
      cluster: null,
      state: "open",
    });
  });

  it("links to the queue with only what differs from the defaults", () => {
    expect(reviewHref("demo")).toBe("/p/demo/kg/review");
    expect(reviewHref("demo", { rule: "bulk_noise", cluster: "c 1", state: "open" })).toBe("/p/demo/kg/review?rule=bulk_noise&cluster=c+1");
    expect(reviewHref("demo", { state: "dismissed" })).toBe("/p/demo/kg/review?state=dismissed");
  });

  it("keys each view of the queue apart under one key a dismissal refreshes", () => {
    const open = kgKeys.reviewList("demo", { rule: null, cluster: null, state: "open" });
    const all = kgKeys.reviewList("demo", { rule: null, cluster: null, state: "all" });
    expect(open).not.toEqual(all);
    expect(open.slice(0, 3)).toEqual([...kgKeys.review("demo")]);
  });

  it("names the first nodes of an item and counts the rest", () => {
    const many = item("g", "possible_duplicate", "c1", 0, {
      nodes: [
        { id: "problem:1", kind: "Problem", name: "Slow search" },
        { id: "problem:2", kind: "Problem", name: null },
        { id: "problem:3", kind: "Problem", name: "slow SEARCH" },
        { id: "problem:4", kind: "Problem", name: "Slow search!" },
      ],
    });
    expect(nodeNames(many)).toEqual({ names: ["Slow search", "problem:2", "slow SEARCH"], more: 1 });
  });
});
