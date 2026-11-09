import { describe, expect, it } from "vitest";

import { finding, item, ITEMS } from "@/test/fixtures/srs";

import type { SrsItem } from "./api";
import { approveGate, caseAccess, caseFindings, fieldParts, fieldRows, fieldView, keysUnder, optionKey, ruleRows, shownCase, stepsChanged } from "./case-review-model";

const before = ITEMS.find((one) => one.key === "uc1")!;
/** The use case as the version under review reads it: an intent and a context written, the actor reworded, a step added. */
const after: SrsItem = {
  ...before,
  fields: {
    ...before.fields,
    objective: undefined,
    intent: "The customer turns a full cart into an order",
    actor: "Signed-in customer",
    context: "The shop is open",
    steps: [...(before.fields.steps as object[]), { step: "3", actor: "", system: "Reserves the stock" }],
  },
};

describe("a use case's review", () => {
  it("lists the fields in plain order, each marked against the version approved last", () => {
    const rows = fieldRows(before, after);
    expect(rows.map((row) => [row.key, row.change])).toEqual([
      ["actor", "changed"],
      ["intent", "changed"],
      ["context", "added"],
    ]);
    // The intent of an older use case is its objective.
    expect(rows[1].before).toBe("The customer turns a cart into an order");
    expect(rows[1].after).toBe("The customer turns a full cart into an order");
  });

  it("marks nothing when there is no use case on one side, and shows the text it has", () => {
    expect(fieldRows(null, after).every((row) => row.change === "same" && row.before === null)).toBe(true);
    const dropped = fieldRows(before, null);
    expect(dropped.map((row) => [row.key, row.after])).toEqual([
      ["actor", "Customer"],
      ["intent", "The customer turns a cart into an order"],
    ]);
    expect(fieldRows(before, before).every((row) => row.change === "same")).toBe(true);
  });

  it("marks the words a field changed, or replaces it whole when little is kept", () => {
    expect(fieldParts("The customer turns a cart into an order", "The customer turns a full cart into an order")).toEqual([
      { kind: "same", text: "The customer turns a " },
      { kind: "add", text: "full " },
      { kind: "same", text: "cart into an order" },
    ]);
    expect(fieldParts("Customer", "Signed-in shopper")).toEqual([
      { kind: "del", text: "Customer" },
      { kind: "add", text: "Signed-in shopper" },
    ]);
  });

  it("reads a long rewrite as rewritten, and a short field or a small change word by word", () => {
    const long = "The shopper picks the items, the address and the payment, and the shop holds the stock until it is paid. ".repeat(3);
    const rewrite = "Northwind Billing sends the invoice once the payment clears, then the warehouse ships the parcel and tells the buyer. ".repeat(3);
    expect(fieldView(long, rewrite)).toEqual({ kind: "rewritten" });
    expect(fieldView(long, long.replace("the address", "the delivery address")).kind).toBe("words");
    expect(fieldView("Customer", "Signed-in shopper").kind).toBe("words");
  });

  it("marks the business rules added, changed and removed, and the steps changed", () => {
    const rule2 = item("br2", { kind: "business_rule", code: "BR ORD1-2", title: "Stock", parent: "uc1", heading_level: 5, body: "- Reserve the stock." });
    const reworded = { ...ITEMS.find((one) => one.key === "br1")!, body: "- A cart holds at most 40 lines." };
    const now = [...ITEMS.filter((one) => one.key !== "br1"), reworded, rule2];
    const { rows, removed } = ruleRows(ITEMS, before, now, after);
    expect(rows.map((row) => [row.rule.key, row.change])).toEqual([
      ["br1", "changed"],
      ["br2", "added"],
    ]);
    expect(removed).toEqual([]);
    expect(ruleRows(now, before, ITEMS, after).removed.map((rule) => rule.key)).toEqual(["br2"]);
    expect(ruleRows(ITEMS, null, ITEMS, after).rows.every((row) => row.change === "same")).toBe(true);
    expect(stepsChanged(before, after)).toBe(true);
    expect(stepsChanged(before, before)).toBe(false);
    expect(stepsChanged(null, after)).toBe(false);
  });

  it("reads the use case in this version, else as the version approved last holds it", () => {
    expect(shownCase({ item: after, items: [after], base: before, baseItems: ITEMS })?.item).toBe(after);
    expect(shownCase({ item: null, items: [], base: before, baseItems: ITEMS })?.items).toBe(ITEMS);
    expect(shownCase({ item: null, items: [], base: null, baseItems: [] })).toBeNull();
  });

  it("keeps the findings about the use case or a rule under it, and those to decide about the whole document", () => {
    const keys = keysUnder(ITEMS, "uc1");
    expect([...keys].sort()).toEqual(["br1", "uc1"]);
    const groups = caseFindings(
      [
        finding(3, { item: "uc1", level: "advisory" }),
        finding(2),
        finding(4, { item: "nfr1" }),
        finding(5, { item: null, level: "info" }),
        finding(6, { item: "br1", level: "info" }),
        finding(1, { item: "uc1", status: "resolved" }),
        finding(7, { item: null }),
      ],
      keys,
    );
    expect(groups.decide.map((one) => one.id)).toEqual([2]);
    expect(groups.advice.map((one) => one.id)).toEqual([3, 6]);
    // One about another item (nfr1) holds that item's use case and the version, not this use case.
    expect(groups.elsewhere.map((one) => one.id)).toEqual([7]);
  });

  it("says what holds Agree: @ba still checking first, then the points to settle", () => {
    const groups = caseFindings([finding(2), finding(4, { item: "nfr1" }), finding(5, { item: null })], keysUnder(ITEMS, "uc1"));
    expect(approveGate({ id: 9, state: "running" }, groups)).toEqual({ kind: "ba" });
    expect(approveGate({ id: 9, state: "done" }, groups)).toEqual({ kind: "findings", here: 1, elsewhere: 1 });
    expect(approveGate(null, caseFindings([finding(4, { item: "nfr1" })], keysUnder(ITEMS, "uc1")))).toBeNull();
    expect(approveGate(null, caseFindings([], new Set()))).toBeNull();
    expect(approveGate({ id: 9, state: "failed" }, null)).toBeNull();
  });

  it("says why the viewer may not review, in the hub's order", () => {
    const base = { signedIn: true, block: null, configPending: false, toReview: true } as const;
    expect(caseAccess(base)).toBe("review");
    expect(caseAccess({ ...base, signedIn: false })).toBe("waiting");
    expect(caseAccess({ ...base, block: "reader" })).toBe("reader");
    expect(caseAccess({ ...base, block: "not_in_review" })).toBe("closed");
    expect(caseAccess({ ...base, block: "author", configPending: true })).toBe("waiting");
    expect(caseAccess({ ...base, block: "author" })).toBe("author");
    expect(caseAccess({ ...base, toReview: false })).toBe("unchanged");
  });

  it("names the choices A to D", () => {
    expect([0, 1, 2, 3].map(optionKey)).toEqual(["A", "B", "C", "D"]);
    expect(optionKey(4)).toBe("5");
  });
});
