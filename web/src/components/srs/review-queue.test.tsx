// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { queryKeys } from "@/lib/queries";
import { aUseCase, caseReview, queued } from "@/test/fixtures/srs";
import { renderVi } from "@/test/render";

import { type SrsQueuedVersion, srsKeys } from "./api";
import { ReviewQueuePage } from "./review-queue-page";

const PROJECT = "demo";

/** A client that holds the queue and the visitor bao, a writer of the project, and never asks the API. */
function show(queue: SrsQueuedVersion[]) {
  const client = new QueryClient({ defaultOptions: { queries: { staleTime: Infinity, retry: false } } });
  client.setQueryData(srsKeys.reviews(PROJECT), queue);
  client.setQueryData(queryKeys.whoami, {
    login: "bao",
    admin: false,
    token: { kind: "web", scopes: [], expires_at: null },
    grants: [{ project: PROJECT, role: "writer", max_level: "internal" }],
  });
  return renderVi(
    <QueryClientProvider client={client}>
      <ReviewQueuePage project={PROJECT} title="SRS reviews" initialError={null} />
    </QueryClientProvider>,
  );
}

beforeEach(() => vi.stubGlobal("fetch", vi.fn(() => Promise.reject(new Error("no API in unit tests")))));
afterEach(() => vi.unstubAllGlobals());

const SHOP = queued({
  held_by: "findings",
  deciding: 2,
  use_cases: [
    aUseCase({ key: "uc1" }),
    aUseCase({ key: "uc2", code: "UC ORD-2", title: "Cancel an order", reviews: [caseReview({ reviewer: "bao" })] }),
  ],
});
const BILLING = queued({
  document: "northwind-billing",
  document_title: "Northwind Billing",
  number: 5,
  round: 2,
  author: "chi",
  held_by: "ba_review",
  review_run: { id: 431, state: "running" },
  use_cases: [aUseCase({ key: "b1", code: "UC BIL-1", title: "Refund a payment", change: "added" })],
});

describe("ReviewQueuePage", () => {
  it("lands a reviewer on what waits for them, and shows every version under All with what holds it", async () => {
    const user = userEvent.setup();
    show([SHOP, BILLING]);
    expect(screen.getByTestId("srs-queue")).toHaveAttribute("data-filter", "mine");
    expect(screen.getByTestId("srs-queue-versions-count")).toHaveTextContent("2 version đang chờ duyệt");
    expect(screen.getByTestId("srs-queue-waiting-count")).toHaveTextContent("2 mục chờ bạn duyệt");
    expect(screen.getByRole("button", { name: /Chờ tôi duyệt/ })).toHaveAttribute("aria-pressed", "true");

    // bao reviewed UC ORD-2 already: only UC ORD-1 of demo-shop and UC BIL-1 of billing wait for them
    let cases = screen.getAllByTestId("srs-queue-case");
    expect(cases.map((row) => row.getAttribute("data-key"))).toEqual(["uc1", "b1"]);
    const review = within(cases[0]).getByTestId("srs-queue-review");
    expect(review).toHaveAttribute("href", "/p/demo/x/srs/documents/demo-shop/versions/3#case-uc1");
    expect(within(cases[0]).getByTestId("srs-queue-case-link")).toHaveAttribute("href", "/p/demo/x/srs/documents/demo-shop/versions/3#case-uc1");

    const [shop, billing] = screen.getAllByTestId("srs-queue-version");
    expect(within(shop).getByTestId("srs-queue-hold")).toHaveTextContent("Còn 2 phát hiện cần quyết định chưa chốt");
    expect(within(shop).queryByTestId("srs-queue-round")).toBeNull();
    expect(within(billing).getByTestId("srs-queue-hold")).toHaveTextContent("@ba đang rà soát");
    expect(within(billing).getByTestId("srs-review-run")).toHaveAttribute("data-run-id", "431");
    expect(within(billing).getByTestId("srs-queue-round")).toHaveTextContent("Vòng 2");
    expect(within(billing).getByTestId("srs-queue-byline")).toHaveTextContent("Người viết chi");

    await user.click(screen.getByRole("button", { name: /Tất cả/ }));
    expect(screen.getByTestId("srs-queue")).toHaveAttribute("data-filter", "all");
    cases = screen.getAllByTestId("srs-queue-case");
    expect(cases.map((row) => row.getAttribute("data-key"))).toEqual(["uc1", "uc2", "b1"]);
    expect(within(cases[1]).getByTestId("srs-queue-reviewer")).toHaveTextContent("bao");
    expect(within(cases[1]).getByTestId("srs-queue-reviewer")).toHaveTextContent("bạn");
  });

  it("says when nothing waits for the reviewer, and when nothing is in review at all", async () => {
    const user = userEvent.setup();
    const done = queued({ use_cases: [aUseCase({ reviews: [caseReview({ reviewer: "bao" })] })] });
    const { unmount } = show([done]);
    expect(screen.getByTestId("srs-queue-mine-empty")).toHaveTextContent("Không có gì chờ bạn duyệt");
    await user.click(screen.getByTestId("srs-queue-show-all"));
    expect(screen.getAllByTestId("srs-queue-case")).toHaveLength(1);
    expect(screen.getByTestId("srs-queue-case")).toHaveAttribute("data-state", "pending");
    unmount();

    show([]);
    expect(screen.getByTestId("srs-queue-empty")).toHaveTextContent("Không có version nào đang chờ duyệt");
    expect(screen.queryByTestId("srs-queue-filter")).toBeNull();
  });

  it("lists a reader's queue under All, with no Review", () => {
    show([queued({ may_review: false, held_by: null })]);
    expect(screen.getByTestId("srs-queue")).toHaveAttribute("data-filter", "all");
    expect(screen.getByTestId("srs-queue-hold")).toHaveTextContent("Sẵn sàng duyệt");
    expect(screen.queryByTestId("srs-queue-review")).toBeNull();
    expect(screen.queryByTestId("srs-queue-waiting-count")).toBeNull();
  });
});
