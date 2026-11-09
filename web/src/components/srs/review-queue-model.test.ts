import { describe, expect, it } from "vitest";

import { aQueuedCase, caseReview, queued, review } from "@/test/fixtures/srs";

import { caseHref, reviewsHref } from "./links";
import { baLine, cardStatus, clip, nextCard, plainLine, queueCards, waitsForViewer } from "./review-queue-model";

describe("the review queue", () => {
  const open = aQueuedCase({ key: "uc1" });
  const reviewed = aQueuedCase({ key: "uc2", code: "UC ORD-2", reviews: [caseReview({ reviewer: "bao" })] });
  const approved = aQueuedCase({ key: "uc3", code: "UC ORD-3", state: "approved", reviews: [caseReview({ reviewer: "chi" })] });
  const asked = aQueuedCase({
    key: "uc4",
    code: "UC ORD-4",
    state: "changes_requested",
    reviews: [caseReview({ reviewer: "chi", decision: "request_changes", body: "Say who pays." })],
  });
  const mineAsked = aQueuedCase({ key: "uc5", state: "changes_requested", reviews: [caseReview({ reviewer: "bao", decision: "request_changes" })] });
  const deciding = aQueuedCase({ key: "uc6", code: "UC ORD-6", deciding: 2, held_by: "findings" });
  const shop = queued({ use_cases: [open, reviewed, approved, asked, mineAsked, deciding], held_by: "findings", deciding: 2 });
  const billing = queued({
    document: "northwind-billing",
    document_title: "Northwind Billing",
    number: 5,
    round: 2,
    may_review: false,
    use_cases: [aQueuedCase({ key: "b1", code: "UC BIL-1", may_review: false })],
  });
  const terms = queued({ document: "northwind-terms", document_title: "Northwind Terms", number: 2, use_cases: [] });
  const checking = queued({
    document: "northwind-ledger",
    number: 7,
    held_by: "ba_review",
    review_run: { id: 431, state: "running" },
    use_cases: [aQueuedCase({ held_by: "ba_review" })],
  });

  it("says where each use case stands for the viewer, in plain words", () => {
    expect(cardStatus(shop, mineAsked, "bao")).toEqual({ kind: "asked" });
    expect(cardStatus(shop, reviewed, "bao")).toEqual({ kind: "approved" });
    expect(cardStatus(shop, approved, "bao")).toEqual({ kind: "approved" });
    expect(cardStatus(billing, billing.use_cases[0], "bao")).toEqual({ kind: "others" });
    expect(cardStatus(shop, asked, "bao")).toEqual({ kind: "changes" });
    expect(cardStatus(checking, checking.use_cases[0], "bao")).toEqual({ kind: "ba_review" });
    expect(cardStatus(shop, deciding, "bao")).toEqual({ kind: "deciding", count: 2 });
    // Points to decide about another use case hold that one, not this one.
    expect(cardStatus(shop, open, "bao")).toEqual({ kind: "waiting" });
    expect(cardStatus(queued(), open, "bao")).toEqual({ kind: "waiting" });
    expect(cardStatus(shop, reviewed, "erin")).toEqual({ kind: "waiting" });
    // A point about the whole document holds every use case, though it counts for none of them.
    expect(cardStatus(shop, { ...open, held_by: "findings" }, "bao")).toEqual({ kind: "deciding", count: 1 });
    // Someone who wrote a revision of this use case may not review it, yet may review the others.
    expect(cardStatus(shop, { ...open, may_review: false }, "bao")).toEqual({ kind: "others" });
    expect(cardStatus(shop, { ...open, state: "superseded" }, "bao")).toEqual({ kind: "approved" });
  });

  it("gives a version that changes no use case one card, waiting until the viewer reviews its round", () => {
    expect(cardStatus(terms, null, "bao")).toEqual({ kind: "waiting" });
    expect(cardStatus({ ...terms, reviews: [review({ reviewer: "bao", round: 1 })] }, null, "bao")).toEqual({ kind: "approved" });
    expect(cardStatus({ ...terms, round: 2, reviews: [review({ reviewer: "bao", round: 1 })] }, null, "bao")).toEqual({ kind: "waiting" });
    expect(cardStatus({ ...terms, may_review: false }, null, "bao")).toEqual({ kind: "others" });
  });

  it("puts what waits for the viewer first, then what waits for @ba or others, each in the queue's order", () => {
    const { mine, others } = queueCards([shop, billing, terms, checking], "bao");
    expect(mine.map((card) => [card.version.document, card.useCase?.key ?? null])).toEqual([
      ["demo-shop", "uc1"],
      ["demo-shop", "uc6"],
      ["northwind-terms", null],
    ]);
    expect(others.map((card) => [card.version.document, card.useCase?.key ?? null, card.status.kind])).toEqual([
      ["demo-shop", "uc2", "approved"],
      ["demo-shop", "uc3", "approved"],
      ["demo-shop", "uc4", "changes"],
      ["demo-shop", "uc5", "asked"],
      ["northwind-billing", "b1", "others"],
      ["northwind-ledger", "uc1", "ba_review"],
    ]);
    expect(queueCards([], "bao")).toEqual({ mine: [], others: [] });
    expect(waitsForViewer({ kind: "deciding", count: 1 })).toBe(true);
    expect(waitsForViewer({ kind: "ba_review" })).toBe(false);
  });

  it("finds the next use case waiting for the viewer after the one they decided", () => {
    const queue = [shop, billing, terms];
    expect(nextCard(queue, "bao", { document: "demo-shop", number: 3, key: "uc1" })?.useCase?.key).toBe("uc6");
    expect(nextCard(queue, "bao", { document: "demo-shop", number: 3, key: "uc6" })?.version.document).toBe("northwind-terms");
    expect(nextCard(queue, "bao", { document: "northwind-terms", number: 2, key: null })?.useCase?.key).toBe("uc1");
    // A use case no longer waiting (just approved) is not in the list: the first waiting one comes next.
    expect(nextCard(queue, "bao", { document: "demo-shop", number: 3, key: "uc2" })?.useCase?.key).toBe("uc1");
    expect(nextCard([queued()], "bao", { document: "demo-shop", number: 3, key: "uc1" })).toBeNull();
  });

  it("says what @ba's check of the version found", () => {
    expect(baLine(checking, checking.use_cases[0])).toEqual({ kind: "running" });
    expect(baLine(shop, deciding)).toEqual({ kind: "here", count: 2 });
    expect(baLine(shop, open)).toEqual({ kind: "elsewhere", count: 2 });
    expect(baLine(queued({ review_run: { id: 9, state: "done" } }), open)).toEqual({ kind: "clear" });
    expect(baLine(queued({ review_run: { id: 9, state: "failed" } }), open)).toEqual({ kind: "broke" });
    // A project without @ba: nothing to say.
    expect(baLine(queued(), open)).toBeNull();
  });

  it("cuts a long line at a word, as one plain line", () => {
    expect(plainLine("**Cap** the `cart`\n at [50 lines](https://example.org)")).toBe("Cap the cart at 50 lines");
    expect(clip("Short", 160)).toBe("Short");
    const long = "word ".repeat(60);
    const cut = clip(long, 160);
    expect(cut.length).toBeLessThanOrEqual(160);
    expect(cut.endsWith("word…")).toBe(true);
  });

  it("links the queue, and a use case to its own review", () => {
    expect(reviewsHref("demo")).toBe("/p/demo/x/srs/reviews");
    expect(caseHref("demo", "demo-shop", 3, "uc1")).toBe("/p/demo/x/srs/documents/demo-shop/versions/3/cases/uc1");
    expect(caseHref("demo", "demo-shop", 3, "a b/c")).toBe("/p/demo/x/srs/documents/demo-shop/versions/3/cases/a%20b%2Fc");
  });
});
