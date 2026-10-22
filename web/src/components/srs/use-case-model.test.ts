import { describe, expect, it } from "vitest";

import { aUseCase, caseReview, ITEMS } from "@/test/fixtures/srs";

import { carriedFrom, caseBlock, caseName, caseOf, caseRef, caseVersion, holdsVersion, ownReview, summarize } from "./use-case-model";

describe("the use cases a version needs reviewed", () => {
  const cases = [
    aUseCase({ key: "a", state: "approved" }),
    aUseCase({ key: "b", state: "pending" }),
    aUseCase({ key: "c", state: "changes_requested" }),
    aUseCase({ key: "d", state: "approved" }),
  ];

  it("counts where each stands, and says when one holds the version", () => {
    expect(summarize(cases)).toEqual({ total: 4, approved: 2, pending: 1, asked: 1 });
    expect(summarize([])).toEqual({ total: 0, approved: 0, pending: 0, asked: 0 });
    expect(holdsVersion(cases)).toBe(true);
    expect(holdsVersion(cases.filter((one) => one.state !== "changes_requested"))).toBe(false);
  });

  it("lets a reviewer review a use case again, whatever they said on the version", () => {
    expect(caseBlock("reviewed")).toBeNull();
    expect(caseBlock(null)).toBeNull();
    expect(caseBlock("author")).toBe("author");
    expect(caseBlock("reader")).toBe("reader");
    expect(caseBlock("not_in_review")).toBe("not_in_review");
  });

  it("names a use case by its code, else its title or key", () => {
    expect(caseName(aUseCase())).toBe("UC ORD-1");
    expect(caseRef(aUseCase())).toBe("UC ORD-1");
    expect(caseName(aUseCase({ code: null }))).toBe("Place an order");
    expect(caseRef(aUseCase({ code: null, key: "uc9" }))).toBe("uc9");
  });

  it("finds the review of a person that counts, and the version a kept review was recorded on", () => {
    const one = aUseCase({ reviews: [caseReview({ reviewer: "bao", version: 2 }), caseReview({ reviewer: "chi" })] });
    expect(ownReview(one, "bao")?.version).toBe(2);
    expect(ownReview(one, "dung")).toBeNull();
    expect(ownReview(one, null)).toBeNull();
    expect(carriedFrom(one.reviews[0], 3)).toBe(2);
    expect(carriedFrom(one.reviews[1], 3)).toBeNull();
  });

  it("opens a dropped use case at the version approved last, which still holds it", () => {
    expect(caseVersion(aUseCase(), 3, 2)).toBe(3);
    expect(caseVersion(aUseCase({ change: "added" }), 3, null)).toBe(3);
    expect(caseVersion(aUseCase({ change: "removed" }), 3, 2)).toBe(2);
    expect(caseVersion(aUseCase({ change: "removed" }), 3, null)).toBeNull();
  });

  it("matches an item with its use case by key, and no other kind of item", () => {
    const uc = ITEMS.find((one) => one.key === "uc1")!;
    const rule = ITEMS.find((one) => one.key === "br1")!;
    expect(caseOf([aUseCase()], uc)?.key).toBe("uc1");
    expect(caseOf([aUseCase({ key: "uc2" })], uc)).toBeNull();
    expect(caseOf([aUseCase({ key: "br1" })], rule)).toBeNull();
  });
});
