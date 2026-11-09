// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { screen, within } from "@testing-library/react";
import type { ReactElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { queryKeys } from "@/lib/queries";
import { aUseCase, documentOf, ITEMS, version } from "@/test/fixtures/srs";
import { renderVi } from "@/test/render";

import { type SrsComment, srsKeys } from "./api";
import { DocumentPage } from "./document-page";
import { findQuote, flatten, pickedIn, rangeOf, threadsOn, words } from "./passage-model";
import { useCaseLine } from "./use-case-status";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  usePathname: () => "/p/demo/x/srs/documents/demo-shop",
  useSearchParams: () => new URLSearchParams(),
}));

const PROJECT = "demo";

function comment(id: number, patch: Partial<SrsComment> = {}): SrsComment {
  return {
    id,
    version: 3,
    item: "uc1",
    item_code: "UC ORD-1",
    parent_id: null,
    author: "bao",
    run_id: null,
    body: "Why?",
    created_at: "2026-11-09T08:00:00Z",
    resolved_by: null,
    resolved_at: null,
    field: "body",
    quote: null,
    prefix: null,
    suffix: null,
    start_offset: null,
    anchor: null,
    ask_run_id: null,
    ...patch,
  };
}

describe("passages of an item", () => {
  function text(): HTMLElement {
    const root = document.createElement("div");
    root.innerHTML =
      '<p>A cart <strong>holds</strong>   at most 50 lines.</p><article data-srs-item="br2" data-srs-field="body"><p>Stock is held for 15 minutes</p></article>';
    document.body.append(root);
    return root;
  }

  it("reads an element's words across marks and blocks, and finds a passage again by them", () => {
    const root = text();
    const flat = flatten(root);
    expect(flat.text).toBe("A cart holds at most 50 lines. Stock is held for 15 minutes");
    const at = findQuote(flat, "holds  at most 50")!;
    expect(rangeOf(flat, at.start, at.end)!.toString().replace(/\s+/g, " ")).toBe("holds at most 50");
    expect(findQuote(flat, "at most 40")).toBeNull();
    expect(words("  A\n b ")).toBe("a b");
    root.remove();
  });

  it("takes a selection in a business rule drawn inside its use case as a passage of the rule", () => {
    const root = text();
    const range = document.createRange();
    const held = root.querySelector("article p")!.firstChild as Text;
    range.setStart(held, 9);
    range.setEnd(held, 13);
    expect(pickedIn(root, range, "uc1")).toEqual({ item: "br2", field: "body", quote: "held", prefix: "Stock is ", suffix: " for 15 minutes", offset: 9 });
    const outside = document.createRange();
    outside.selectNodeContents(document.body);
    expect(pickedIn(root.querySelector("article")!, outside, "uc1")).toBeNull();
    root.remove();
  });

  it("gathers the threads of the item and the items under it, each with its answers", () => {
    const found = threadsOn([comment(1), comment(2, { parent_id: 1 }), comment(3, { item: "uc2" }), comment(4, { item: "br1" })], new Set(["uc1", "br1"]));
    expect(found.map(({ root, replies }) => [root.id, replies.map((one) => one.id)])).toEqual([
      [1, [2]],
      [4, []],
    ]);
  });
});

describe("the line of a use case", () => {
  const item = { code: "UC ORD-1", title: "Place an order", key: "uc1", rev: 3, released: 2 };

  it("names its revision, links its review while it waits and lets a writer send a new revision of the latest draft", () => {
    const inReview = version(3, { state: "in_review" });
    expect(useCaseLine(item, aUseCase({ state: "pending" }), inReview, inReview, true)).toEqual({
      label: "UC ORD-1 r3",
      state: "pending",
      approved: 2,
      review: true,
      submit: false,
    });
    const draft = version(4, { state: "draft" });
    expect(useCaseLine(item, aUseCase({ state: "pending" }), draft, draft, true)).toMatchObject({ review: false, submit: true });
    expect(useCaseLine(item, aUseCase({ state: "pending", submitted: true }), draft, draft, true)).toMatchObject({ review: true, submit: false });
    expect(useCaseLine(item, null, draft, draft, false)).toMatchObject({ submit: false });
    expect(useCaseLine({ ...item, released: 3 }, null, draft, draft, true)).toMatchObject({ submit: false, approved: 3 });
  });
});

describe("the contents of a use case", () => {
  function seeded(comments: SrsComment[]) {
    const client = new QueryClient({ defaultOptions: { queries: { staleTime: Infinity, retry: false } } });
    const doc = documentOf();
    const items = ITEMS.map((one) => (one.key === "uc1" ? { ...one, rev: 2, released: 1 } : one));
    const shown = { ...doc.shown!, items, use_cases: [aUseCase({ key: "uc1", rev: 2, released: 1, state: "pending" })] };
    client.setQueryData(srsKeys.version(PROJECT, "demo-shop", null), { ...doc, shown });
    client.setQueryData(srsKeys.comments(PROJECT, "demo-shop"), comments);
    client.setQueryData(queryKeys.whoami, {
      login: "bao",
      admin: false,
      token: { kind: "web", scopes: [], expires_at: null },
      grants: [{ project: PROJECT, role: "writer", max_level: "internal" }],
    });
    return client;
  }

  function show(ui: ReactElement, client: QueryClient) {
    return renderVi(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
  }

  beforeEach(() => vi.stubGlobal("fetch", vi.fn(() => Promise.reject(new Error("no API in unit tests")))));
  afterEach(() => vi.unstubAllGlobals());

  it("says its revision and its review, and lists the threads on its passages, one whose passage changed", async () => {
    const threads = [comment(7, { quote: "words no longer there" }), comment(8, { parent_id: 7, author: "chi", body: "Still?" })];
    show(<DocumentPage project={PROJECT} slug="demo-shop" asked={{ version: 3, item: "uc1", edit: false }} initialError={null} />, seeded(threads));
    const line = screen.getByTestId("srs-use-case-line");
    expect(within(line).getByTestId("srs-use-case-rev")).toHaveTextContent("UC ORD-1 r2");
    expect(within(line).getByTestId("srs-use-case-approved")).toHaveTextContent("Đã duyệt: r1");
    expect(within(line).getByTestId("srs-use-case-review")).toHaveAttribute("href", "/p/demo/x/srs/documents/demo-shop/versions/3/cases/uc1");
    const panel = screen.getByTestId("srs-passages");
    const thread = within(panel).getByTestId("srs-passage-thread");
    expect(thread).toHaveTextContent("words no longer there");
    expect(within(thread).getAllByTestId("srs-comment")).toHaveLength(2);
    expect(await within(thread).findByTestId("srs-passage-changed")).toHaveTextContent("Đoạn đã đổi");
  });
});
