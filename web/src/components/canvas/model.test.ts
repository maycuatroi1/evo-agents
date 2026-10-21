import { describe, expect, it } from "vitest";

import { ApiError } from "@/lib/api/errors";

import type { CanvasContent } from "./api";
import { CELL_PLACE, CELLS, changeOf, codesOf, conflictsOf, emptyForm, formOf, isOverdue, rebase, slugOf, statusCounts, testCardOf, testingLacks, utcToday, valueText } from "./model";

const CONTENT: CanvasContent = {
  cell: "problem",
  position: 1,
  text: "Shop owners lose a sale when checkout takes more than two minutes",
  status: "assumption",
  confidence: "low",
  test_card: {
    hypothesis: "Owners pay for one-click checkout",
    test: "Offer it to 20 shops",
    metric: "checkout.conversion",
    aggregate: "mean",
    window_days: 30,
    comparator: ">=",
    threshold: 0.4,
    min_samples: null,
    tenant: null,
    deadline: "2026-11-30",
  },
  archived: false,
  use_cases: ["UC ORD-1"],
  signal_keys: ["checkout.conversion"],
};

describe("the cells of the canvas", () => {
  it("come in the order Lean Canvas numbers them, each with a place on the printed canvas", () => {
    expect(CELLS).toEqual([
      "problem",
      "customer_segments",
      "unique_value_proposition",
      "solution",
      "channels",
      "revenue_streams",
      "cost_structure",
      "key_metrics",
      "unfair_advantage",
    ]);
    for (const cell of CELLS) expect(CELL_PLACE[cell]).toMatch(/xl:col-start-\d+/);
  });
});

describe("an edit of a claim", () => {
  it("sends only the fields that differ, with the rev it was read at", () => {
    const form = { ...formOf(CONTENT), text: "Owners lose a sale after two minutes", status: "testing" as const };
    expect(changeOf(form, { content: CONTENT, code: "CL-3", rev: 4 })).toEqual({
      claim: "CL-3",
      base_rev: 4,
      text: "Owners lose a sale after two minutes",
      status: "testing",
    });
  });

  it("is nothing when the form holds the claim as it is", () => {
    expect(changeOf(formOf(CONTENT), { content: CONTENT, code: "CL-3", rev: 4 })).toBeNull();
  });

  it("sends the whole test card when one of its fields changes, and links as whole lists", () => {
    const form = { ...formOf(CONTENT), threshold: "0.5", useCases: "UC ORD-1, UC ORD-2", signalKeys: [] };
    const change = changeOf(form, { content: CONTENT, code: "CL-3", rev: 4 });
    expect(change?.test_card).toMatchObject({ threshold: 0.5, metric: "checkout.conversion", deadline: "2026-11-30" });
    expect(change?.use_cases).toEqual(["UC ORD-1", "UC ORD-2"]);
    expect(change?.signal_keys).toEqual([]);
  });

  it("makes a new claim from every field, with no test card while its fields are empty", () => {
    const form = { ...emptyForm("channels"), text: "  Shops find us through accountants  " };
    expect(changeOf(form, null)).toEqual({
      cell: "channels",
      text: "Shops find us through accountants",
      status: "assumption",
      confidence: "low",
      test_card: null,
      use_cases: [],
      signal_keys: [],
    });
  });
});

describe("an edit carried over to a newer rev", () => {
  it("keeps what the writer changed and takes the rest from the newer rev", () => {
    const newer: CanvasContent = { ...CONTENT, text: "Owners lose a sale after two minutes", signal_keys: [] };
    const mine = { ...formOf(CONTENT), confidence: "high" as const, threshold: "0.5" };
    const merged = rebase(mine, CONTENT, newer);
    expect(merged).toMatchObject({ text: "Owners lose a sale after two minutes", confidence: "high", threshold: "0.5", signalKeys: [] });
    expect(changeOf(merged, { content: newer, code: "CL-3", rev: 5 })).toMatchObject({ claim: "CL-3", base_rev: 5, confidence: "high" });
    expect(changeOf(merged, { content: newer, code: "CL-3", rev: 5 })?.text).toBeUndefined();
  });
});

describe("a test card in a form", () => {
  it("reads numbers and leaves out what is not one", () => {
    const card = testCardOf({ ...emptyForm("problem"), windowDays: "14", threshold: "abc", minSamples: "2.5", hypothesis: " x " });
    expect(card).toMatchObject({ window_days: 14, threshold: null, min_samples: null, hypothesis: "x" });
  });

  it("names what testing lacks", () => {
    expect(testingLacks(null)).toEqual(["hypothesis", "test", "deadline"]);
    expect(testingLacks({ hypothesis: "h", test: "t", deadline: "2026-11-30" })).toEqual([]);
  });

  it("splits use cases on commas and lines, once each", () => {
    expect(codesOf("UC ORD-1, UC ORD-2\nUC ORD-1,,")).toEqual(["UC ORD-1", "UC ORD-2"]);
  });
});

describe("an overdue test card", () => {
  const claim = { status: "testing" as const, test_card: CONTENT.test_card, real_support_count: 0 };

  it("is one past its deadline with no real evidence, while the claim is an assumption or in testing", () => {
    expect(isOverdue(claim, "2026-12-01")).toBe(true);
    expect(isOverdue(claim, "2026-11-30")).toBe(false);
    expect(isOverdue({ ...claim, real_support_count: 1 }, "2026-12-01")).toBe(false);
    expect(isOverdue({ ...claim, status: "validated" }, "2026-12-01")).toBe(false);
    expect(isOverdue({ ...claim, test_card: null }, "2026-12-01")).toBe(false);
  });

  it("is told by the day in UTC", () => {
    expect(utcToday(new Date("2026-10-21T23:30:00-07:00"))).toBe("2026-10-22");
  });
});

describe("a 409 of an edit", () => {
  it("names each claim that changed, who changed it and what", () => {
    const error = new ApiError({
      status: 409,
      code: "conflict",
      message: "CL-3 changed since you read them",
      requestId: null,
      detail: [{ claim: "CL-3", base_rev: 4, rev: 5, changed_by: "mai", base: {}, current: {}, changes: [{ field: "text", before: "a", after: "b" }] }],
    });
    expect(conflictsOf(error)).toEqual([{ claim: "CL-3", baseRev: 4, rev: 5, changedBy: "mai", changes: [{ field: "text", before: "a", after: "b" }] }]);
  });

  it("is nothing for any other failure", () => {
    expect(conflictsOf(new ApiError({ status: 422, code: "invalid", message: "x", requestId: null }))).toEqual([]);
    expect(conflictsOf(new Error("x"))).toEqual([]);
  });
});

describe("what the pages count and show", () => {
  it("counts every status, a missing one as zero", () => {
    expect(statusCounts({ assumption: 2, validated: 1 })).toEqual({ assumption: 2, testing: 0, validated: 1, invalidated: 0, retired: 0 });
  });

  it("writes a field's value as text", () => {
    expect(valueText(["UC ORD-1", "UC ORD-2"])).toBe("UC ORD-1, UC ORD-2");
    expect(valueText(null)).toBe("");
    expect(valueText(0.4)).toBe("0.4");
  });

  it("makes a slug from a product's name", () => {
    expect(slugOf("Northwind Checkout")).toBe("northwind-checkout");
    expect(slugOf("Đơn hàng Lumen!")).toBe("don-hang-lumen");
  });
});
