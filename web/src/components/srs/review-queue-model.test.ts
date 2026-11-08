import { describe, expect, it } from "vitest";

import { aUseCase, caseReview, queued, review } from "@/test/fixtures/srs";

import { caseReviewHref, reviewsHref } from "./links";
import { caseWaitsFor, defaultFilter, mayReviewCase, queueCounts, queueGroups, toReview, versionWaitsFor } from "./review-queue-model";

describe("the review queue", () => {
  const open = aUseCase({ key: "uc1" });
  const reviewed = aUseCase({ key: "uc2", code: "UC ORD-2", reviews: [caseReview({ reviewer: "bao" })] });
  const approved = aUseCase({ key: "uc3", code: "UC ORD-3", state: "approved", reviews: [caseReview({ reviewer: "chi" })] });
  const asked = aUseCase({
    key: "uc4",
    code: "UC ORD-4",
    state: "changes_requested",
    reviews: [caseReview({ reviewer: "chi", decision: "request_changes", body: "Say who pays." })],
  });
  const shop = queued({ use_cases: [open, reviewed, approved, asked] });
  const billing = queued({
    document: "northwind-billing",
    document_title: "Northwind Billing",
    number: 5,
    round: 2,
    may_review: false,
    use_cases: [aUseCase({ key: "b1", code: "UC BIL-1" })],
  });
  const terms = queued({ document: "northwind-terms", document_title: "Northwind Terms", number: 2, use_cases: [] });

  it("keeps under Waiting for me the pending use cases the viewer may review and has not reviewed", () => {
    expect(caseWaitsFor(shop, open, "bao")).toBe(true);
    expect(caseWaitsFor(shop, reviewed, "bao")).toBe(false);
    expect(caseWaitsFor(shop, reviewed, "dung")).toBe(true);
    expect(caseWaitsFor(shop, approved, "bao")).toBe(false);
    expect(caseWaitsFor(shop, asked, "bao")).toBe(false);
    expect(caseWaitsFor(billing, billing.use_cases[0], "bao")).toBe(false);
    const groups = queueGroups([shop, billing, terms], "mine", "bao");
    expect(groups.map((group) => [group.version.document, group.cases.map((one) => one.key)])).toEqual([
      ["demo-shop", ["uc1"]],
      ["northwind-terms", []],
    ]);
  });

  it("waits on a version that changes no use case until the viewer reviews its round", () => {
    expect(versionWaitsFor(terms, "bao")).toBe(true);
    expect(versionWaitsFor({ ...terms, reviews: [review({ reviewer: "bao", round: 1 })] }, "bao")).toBe(false);
    expect(versionWaitsFor({ ...terms, round: 2, reviews: [review({ reviewer: "bao", round: 1 })] }, "bao")).toBe(true);
    expect(versionWaitsFor({ ...terms, may_review: false }, "bao")).toBe(false);
    expect(versionWaitsFor(shop, "bao")).toBe(false);
  });

  it("shows every version and use case under All, in the queue's order, and counts what each filter holds", () => {
    expect(queueGroups([shop, billing, terms], "all", "bao").map((group) => [group.version.document, group.cases.length])).toEqual([
      ["demo-shop", 4],
      ["northwind-billing", 1],
      ["northwind-terms", 0],
    ]);
    expect(queueCounts([shop, billing, terms], "bao")).toEqual({ versions: 3, all: 6, mine: 2 });
    expect(queueCounts([], "bao")).toEqual({ versions: 0, all: 0, mine: 0 });
    expect(toReview([])).toBe(0);
  });

  it("lands a visitor who may review a version on Waiting for me, anyone else on All", () => {
    expect(defaultFilter([shop, billing])).toBe("mine");
    expect(defaultFilter([billing])).toBe("all");
    expect(defaultFilter([])).toBe("all");
  });

  it("offers Review on a pending use case of a version the viewer may review", () => {
    expect(mayReviewCase(shop, reviewed)).toBe(true);
    expect(mayReviewCase(shop, approved)).toBe(false);
    expect(mayReviewCase(shop, asked)).toBe(false);
    expect(mayReviewCase(billing, billing.use_cases[0])).toBe(false);
  });

  it("links the queue, and a use case to its row on the version's review page", () => {
    expect(reviewsHref("demo")).toBe("/p/demo/x/srs/reviews");
    expect(caseReviewHref("demo", "demo-shop", 3, "uc1")).toBe("/p/demo/x/srs/documents/demo-shop/versions/3#case-uc1");
  });
});
