import type { SrsDocument, SrsFinding, SrsItem, SrsReview, SrsVersion } from "@/components/srs/api";

/** Items, versions and documents for the SRS pages' unit tests: the shape of the demo-shop sample (tests/hub/fixtures/srs). */
export function item(key: string, patch: Partial<SrsItem> = {}): SrsItem {
  return {
    key,
    kind: "section",
    code: null,
    aliases: [],
    title: key,
    parent: null,
    heading_level: 2,
    fields: {},
    body: "",
    refs: [],
    ...patch,
  };
}

export const ITEMS: SrsItem[] = [
  item("approval", { kind: "approval_page", title: "Approval Page", body: "| Role | Name |\n|---|---|\n| BA | Alice |" }),
  item("intro", { title: "1. Introduction", heading_level: 1 }),
  item("purpose", { title: "1.1. Purpose", parent: "intro", body: "This document describes the order service." }),
  item("orders", { title: "2.1. Orders", heading_level: 2 }),
  item("uc1", {
    kind: "use_case",
    code: "UC ORD-1",
    aliases: ["UC-ORD-001"],
    title: "Place an order",
    parent: "orders",
    heading_level: 4,
    body: "| Item | Content |\n|---|---|\n| ID | UC-ORD-001 |\n\n**Activity Flow**\n\n| Step | Actor | System |\n|---|---|---|\n| 1 | Presses Place order | |\n\nThe cart is kept for a day.",
    fields: {
      id: "UC-ORD-001",
      objective: "The customer turns a cart into an order",
      actor: "Customer",
      priority: "High",
      steps: [
        { step: "1", actor: "Presses Place order", system: "" },
        { step: "2", actor: "", system: "Checks the cart (BR ORD1-1)" },
      ],
    },
    refs: [{ rel: "uses_screen", to: "cart", to_code: "SCR-1" }],
  }),
  item("br1", {
    kind: "business_rule",
    code: "BR ORD1-1",
    title: "Cart rules",
    parent: "uc1",
    heading_level: 5,
    body: "- A cart holds at most 50 lines.",
    fields: { steps: ["2"], step_label: "Step (2)" },
  }),
  item("screens", { title: "3. Mockups Screen", heading_level: 1 }),
  item("cart", { kind: "screen", code: "SCR-1", title: "Cart", parent: "screens", heading_level: 3, body: "| Component | Behaviour |\n|---|---|\n| Place order | Starts UC ORD-1 |" }),
  item("checkout", { kind: "screen", code: "SCR-2", title: "Checkout", parent: "screens", heading_level: 3, body: "| Component | Behaviour |\n|---|---|\n| Pay | Card |" }),
  item("nfrs", { title: "5. Non-Functional Requirements", heading_level: 1, body: "| ID | Group | Requirement |\n|---|---|---|", fields: { tail: "Each is checked before a release." } }),
  item("nfr1", { kind: "nfr", code: "NFR-SEC-01", title: "Card data stays out", parent: "nfrs", heading_level: null, body: "| NFR-SEC-01 | Security | Card data stays out |", fields: { cells: ["NFR-SEC-01", "Security", "Card data stays out"] } }),
];

export function review(patch: Partial<SrsReview> = {}): SrsReview {
  return { reviewer: "bao", role: null, decision: "approve", body: "", round: 1, created_at: "2026-10-10T09:00:00Z", ...patch };
}

export function version(number: number, patch: Partial<SrsVersion> = {}): SrsVersion {
  return {
    number,
    parent_number: number > 1 ? number - 1 : null,
    state: "draft",
    round: 0,
    author: "alice",
    drafted_by_run: null,
    summary: `Version ${number}`,
    created_at: "2026-10-10T08:00:00Z",
    submitted_at: null,
    decided_at: null,
    review_run: null,
    reviews: [],
    ...patch,
  };
}

/** A document whose latest version is v3 in review by alice, v2 approved, v1 superseded. */
export function documentOf(patch: Partial<SrsDocument> = {}): SrsDocument {
  const versions = [
    version(3, { state: "in_review", round: 1, author: "alice", submitted_at: "2026-10-11T08:00:00Z" }),
    version(2, { state: "approved", round: 1, author: "chi", reviews: [review({ reviewer: "alice" })] }),
    version(1, { state: "superseded", author: "alice" }),
  ];
  return {
    slug: "demo-shop",
    title: "Demo Shop: Order Service",
    kind: "srs",
    template: "srs-basic",
    approved_version: 2,
    latest_version: 3,
    latest_state: "in_review",
    label: { level: "internal", location: "any", integrity: "U" },
    created_by: "alice",
    created_at: "2026-10-01T08:00:00Z",
    archived_at: null,
    versions,
    shown: { ...versions[0], document: "demo-shop", preamble: "", items: ITEMS, problems: [] },
    ...patch,
  };
}

/** A finding of demo-shop as @ba records one: open, a contradiction to decide about BR ORD1-1, shown by billing's rule. */
export function finding(id: number, patch: Partial<SrsFinding> = {}): SrsFinding {
  return {
    id,
    version: 3,
    item: "br1",
    item_code: "BR ORD1-1",
    kind: "contradiction",
    level: "decide",
    sources: [{ kind: "code", ref: "BR BIL2-1", note: "billing retries 5 times", document: "billing" }],
    summary: "Demo-shop retries a payment 3 times, billing 5.",
    status: "open",
    resolution: null,
    resolution_sources: [],
    settled_by: null,
    settled_at: null,
    chat_link: null,
    run_id: 41,
    created_at: "2026-10-11T08:05:00Z",
    ...patch,
  };
}
