// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { queryKeys } from "@/lib/queries";
import { aQueuedCase, caseReview, queued } from "@/test/fixtures/srs";
import { renderVi } from "@/test/render";

import { type SrsQueuedVersion, srsKeys } from "./api";
import { ReviewQueuePage } from "./review-queue-page";

const PROJECT = "demo";

/** A client that holds the queue and the visitor bao, a writer of the project, and never asks the API. */
function show(queue: SrsQueuedVersion[], role: "writer" | "reader" = "writer") {
  const client = new QueryClient({ defaultOptions: { queries: { staleTime: Infinity, retry: false } } });
  client.setQueryData(srsKeys.reviews(PROJECT), queue);
  client.setQueryData(queryKeys.whoami, {
    login: "bao",
    admin: false,
    token: { kind: "web", scopes: [], expires_at: null },
    grants: [{ project: PROJECT, role, max_level: "internal" }],
  });
  return renderVi(
    <QueryClientProvider client={client}>
      <ReviewQueuePage project={PROJECT} initialError={null} />
    </QueryClientProvider>,
  );
}

beforeEach(() => vi.stubGlobal("fetch", vi.fn(() => Promise.reject(new Error("no API in unit tests")))));
afterEach(() => vi.unstubAllGlobals());

const SHOP = queued({
  author: "erin",
  summary: "Giới hạn giỏ hàng ở 50 dòng và thêm bước kiểm tra tồn kho trước khi đặt",
  held_by: "findings",
  deciding: 2,
  review_run: { id: 40, state: "done" },
  use_cases: [
    aQueuedCase({ key: "uc1", deciding: 2, held_by: "findings", intent: "Khách hàng **đặt** đơn từ giỏ hàng" }),
    aQueuedCase({ key: "uc2", code: "UC ORD-2", title: "Cancel an order", reviews: [caseReview({ reviewer: "bao" })] }),
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
  use_cases: [aQueuedCase({ key: "b1", code: "UC BIL-1", title: "Refund a payment", change: "added", intent: null, held_by: "ba_review" })],
});

describe("ReviewQueuePage", () => {
  it("lists what waits for the reviewer first, each card in plain words", () => {
    show([SHOP, BILLING]);
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Việc cần duyệt");
    expect(screen.getByTestId("srs-queue-waiting-count")).toHaveTextContent("1 mục chờ bạn");

    const mine = screen.getByTestId("srs-queue-mine");
    expect(within(mine).getByRole("heading", { level: 2 })).toHaveTextContent("Chờ bạn");
    const [card] = within(mine).getAllByTestId("srs-queue-case");
    expect(card).toHaveAttribute("data-key", "uc1");
    expect(within(card).getByTestId("srs-queue-status")).toHaveTextContent("Cần chốt 2 điểm trước");
    expect(within(card).getByTestId("srs-queue-byline")).toHaveTextContent("erin gửi");
    expect(within(card).getByRole("heading", { level: 3 })).toHaveTextContent("Place an order");
    expect(card).toHaveTextContent("Demo Shop: Order Service");
    expect(within(card).getByTestId("srs-queue-summary")).toHaveTextContent("Lần này sửa: Giới hạn giỏ hàng ở 50 dòng");
    expect(within(card).getByTestId("srs-queue-intent")).toHaveTextContent("Mục tiêu: Khách hàng đặt đơn từ giỏ hàng");
    expect(within(card).getByTestId("srs-queue-ba")).toHaveTextContent("@ba đã kiểm tra xong, nêu 2 điểm cần bạn chọn");
    const read = within(card).getByTestId("srs-queue-review");
    expect(read).toHaveTextContent("Đọc và duyệt");
    expect(read).toHaveAttribute("href", "/p/demo/x/srs/documents/demo-shop/versions/3/cases/uc1");

    const others = screen.getByTestId("srs-queue-others");
    expect(within(others).getByRole("heading", { level: 2 })).toHaveTextContent("Đang chờ @ba hoặc người khác");
    const cards = within(others).getAllByTestId("srs-queue-case");
    expect(cards.map((one) => one.getAttribute("data-status"))).toEqual(["approved", "ba_review"]);
    expect(within(cards[1]).getByTestId("srs-queue-status")).toHaveTextContent("@ba đang kiểm tra, khoảng 5 phút");
    expect(within(cards[1]).getByTestId("srs-queue-ba")).toHaveTextContent("@ba đang kiểm tra");
    expect(within(cards[1]).getByTestId("srs-queue-change")).toHaveTextContent("Use case mới");
    expect(within(cards[1]).queryByTestId("srs-queue-intent")).toBeNull();
  });

  it("says kindly when nothing waits for the reviewer, and when nothing is in review at all", () => {
    const done = queued({ use_cases: [aQueuedCase({ reviews: [caseReview({ reviewer: "bao" })] })] });
    const { unmount } = show([done]);
    expect(screen.getByTestId("srs-queue-mine-empty")).toHaveTextContent("Không có use case nào chờ bạn duyệt");
    expect(within(screen.getByTestId("srs-queue-others")).getByTestId("srs-queue-status")).toHaveTextContent("Đã duyệt");
    expect(screen.queryByTestId("srs-queue-waiting-count")).toBeNull();
    unmount();

    show([]);
    expect(screen.getByTestId("srs-queue-empty")).toHaveTextContent("Không có use case nào chờ bạn duyệt");
  });

  it("names each use case with its revision, as UC ORD-1 r3", () => {
    show([queued({ use_cases: [aQueuedCase({ rev: 3 })] })]);
    expect(screen.getByTestId("srs-queue-case")).toHaveTextContent("UC ORD-1 r3");
  });

  it("gives someone who may not review a View button and the status Waiting for someone else", () => {
    show([queued({ may_review: false, use_cases: [aQueuedCase({ may_review: false })] })], "reader");
    const card = screen.getByTestId("srs-queue-case");
    expect(within(card).getByTestId("srs-queue-status")).toHaveTextContent("Chờ người khác");
    expect(within(card).getByTestId("srs-queue-review")).toHaveTextContent("Xem");
    expect(screen.getByTestId("srs-queue-mine-empty")).toBeInTheDocument();
  });

  it("gives a version that changes no use case one card that opens the version's review", () => {
    show([queued({ document: "northwind-terms", document_title: "Northwind Terms", number: 2, use_cases: [] })]);
    const card = screen.getByTestId("srs-queue-case");
    expect(within(card).getByRole("heading", { level: 3 })).toHaveTextContent("Phần sửa ngoài use case");
    expect(within(card).getByTestId("srs-queue-status")).toHaveTextContent("Chờ bạn duyệt");
    expect(within(card).getByTestId("srs-queue-review")).toHaveAttribute("href", "/p/demo/x/srs/documents/northwind-terms/versions/2");
  });
});
