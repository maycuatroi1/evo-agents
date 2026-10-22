// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { createApiClient } from "@/lib/api/client";
import { aUseCase, caseReview } from "@/test/fixtures/srs";
import { renderVi } from "@/test/render";

import { ItemReviewCard } from "./item-review-card";
import { type CaseAccess, UseCaseReviews } from "./use-case-reviews";

const api = vi.hoisted(() => ({ seen: [] as Request[], answer: null as null | ((request: Request) => Response) }));

vi.mock("@/lib/api/browser", () => ({
  browserApi: () =>
    createApiClient({
      baseUrl: "http://hub.test",
      fetch: async (request) => {
        api.seen.push(request);
        if (request.url.endsWith("/v1/auth/web/csrf")) return Response.json({ csrf: "csrf-1", header: "X-Evo-CSRF" });
        return api.answer ? api.answer(request) : Response.json({}, { status: 500 });
      },
    }),
}));

/** jsdom has no ResizeObserver; Radix measures with one. */
class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}
vi.stubGlobal("ResizeObserver", ResizeObserverStub);

const PROJECT = "demo";
const REVIEWER: CaseAccess = { kind: "review", login: "bao", roles: [], lock: null };
const CASES = [
  aUseCase(),
  aUseCase({ key: "uc2", code: "UC ORD-2", title: "Cancel an order", change: "added", state: "approved", reviews: [caseReview({ reviewer: "chi", version: 2 })] }),
  aUseCase({ key: "uc3", code: "UC ORD-3", title: "Refund", change: "removed", state: "changes_requested", reviews: [caseReview({ reviewer: "bao", decision: "request_changes", body: "Name the gateway" })] }),
];

function page(access: CaseAccess, useCases = CASES): ReactElement {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={client}>
      <UseCaseReviews project={PROJECT} slug="demo-shop" version={3} round={1} approved={2} useCases={useCases} access={access} />
    </QueryClientProvider>
  );
}

beforeEach(() => {
  api.seen = [];
  api.answer = () => Response.json({ state: "in_review", use_cases: [] });
});

describe("UseCaseReviews", () => {
  it("lists each use case with its change, state, counted reviews and the version a kept review comes from", () => {
    renderVi(page(REVIEWER));
    const rows = screen.getAllByTestId("srs-case");
    expect(rows).toHaveLength(3);
    expect(screen.getByTestId("srs-cases-summary")).toHaveTextContent("Đã duyệt 1 trên 3, 1 đang bị yêu cầu sửa");
    expect(within(rows[0]).getByRole("link", { name: "UC ORD-1" })).toHaveAttribute("href", "/p/demo/x/srs/documents/demo-shop/items/UC%20ORD-1?v=3");
    expect(within(rows[0]).getByTestId("srs-case-state")).toHaveAttribute("data-status", "pending");
    expect(within(rows[1]).getByText("giữ từ v2, use case vẫn như cũ")).toBeInTheDocument();
    // A dropped use case is read at the version approved last.
    expect(within(rows[2]).getByRole("link", { name: "UC ORD-3" })).toHaveAttribute("href", "/p/demo/x/srs/documents/demo-shop/items/UC%20ORD-3?v=2");
    expect(within(rows[2]).getByText("Name the gateway")).toBeInTheDocument();
    expect(screen.getByTestId("srs-cases-holds")).toHaveTextContent("Chưa duyệt được v3");
  });

  it("posts an approval for the use case, by its code", async () => {
    const user = userEvent.setup();
    renderVi(page(REVIEWER));
    await user.click(within(screen.getAllByTestId("srs-case")[0]).getByTestId("srs-case-approve"));
    await waitFor(() => expect(api.seen.some((request) => request.method === "POST" && request.url.includes("/items/UC%20ORD-1/reviews"))).toBe(true));
    const post = api.seen.find((request) => request.method === "POST")!;
    expect(post.url).toBe("http://hub.test/v1/x/srs/demo/documents/demo-shop/versions/3/items/UC%20ORD-1/reviews");
    expect(post.headers.get("X-Evo-CSRF")).toBe("csrf-1");
    expect(await post.clone().json()).toEqual({ decision: "approve", role: null, body: "" });
  });

  it("asks for a note before it requests changes, then sends it with the role picked", async () => {
    const user = userEvent.setup();
    renderVi(page({ kind: "review", login: "bao", roles: ["qa", "dev"], lock: null }));
    await user.selectOptions(screen.getByTestId("srs-cases-role"), "dev");
    const row = screen.getAllByTestId("srs-case")[0];
    await user.click(within(row).getByTestId("srs-case-request"));
    expect(within(row).getByTestId("srs-case-request")).toHaveAttribute("aria-expanded", "true");
    expect(within(row).getByTestId("srs-case-note")).toHaveFocus();
    await user.click(within(row).getByTestId("srs-case-send"));
    expect(within(row).getByRole("alert")).toHaveTextContent("Hãy ghi cần sửa gì.");
    expect(within(row).getByTestId("srs-case-note")).toHaveAttribute("aria-invalid", "true");
    expect(api.seen).toHaveLength(0);
    await user.type(within(row).getByTestId("srs-case-note"), "Say who pays");
    await user.click(within(row).getByTestId("srs-case-send"));
    await waitFor(() => expect(api.seen.some((request) => request.method === "POST")).toBe(true));
    const post = api.seen.find((request) => request.method === "POST")!;
    expect(await post.clone().json()).toEqual({ decision: "request_changes", role: "dev", body: "Say who pays" });
  });

  it("shows the hub's refusal under the use case", async () => {
    const user = userEvent.setup();
    api.answer = () => Response.json({ detail: "held" }, { status: 409 });
    renderVi(page(REVIEWER));
    await user.click(within(screen.getAllByTestId("srs-case")[0]).getByTestId("srs-case-approve"));
    expect(await screen.findByRole("alert")).toBeInTheDocument();
  });

  it("locks Approve with the reason while the hub would refuse it, and keeps Request changes open", () => {
    renderVi(page({ kind: "review", login: "bao", roles: [], lock: { kind: "findings", count: 2, chat: null } }));
    const approve = screen.getAllByTestId("srs-case-approve")[0];
    expect(approve).toBeDisabled();
    const lock = screen.getByTestId("srs-review-lock");
    expect(approve.getAttribute("aria-describedby")).toBe(lock.parentElement!.id);
    expect(screen.getAllByTestId("srs-case-request")[0]).toBeEnabled();
  });

  it("offers no Approve on a use case the person approved already", () => {
    const mine = [aUseCase({ state: "approved", reviews: [caseReview({ reviewer: "bao" })] })];
    renderVi(page(REVIEWER, mine));
    expect(screen.queryByTestId("srs-case-approve")).toBeNull();
    expect(screen.getByTestId("srs-case-request")).toBeInTheDocument();
  });

  it("gives a person who cannot review the reason and no buttons", () => {
    renderVi(page({ kind: "blocked", reason: "Bạn không duyệt được v3" }));
    expect(screen.getByTestId("srs-cases-blocked")).toHaveTextContent("Bạn không duyệt được v3");
    expect(screen.queryByTestId("srs-case-approve")).toBeNull();
    expect(screen.queryByTestId("srs-case-request")).toBeNull();
    expect(screen.getAllByTestId("srs-case")).toHaveLength(3);
  });

  it("says a version with no use case to review is approved by a review of the whole version", () => {
    renderVi(page(REVIEWER, []));
    expect(screen.getByText(/duyệt cả version là version được duyệt/)).toBeInTheDocument();
    expect(screen.queryByTestId("srs-case")).toBeNull();
    expect(screen.queryByTestId("srs-cases-summary")).toBeNull();
  });
});

describe("ItemReviewCard", () => {
  it("says where the use case stands in the version in review, with who reviewed it and the review page", () => {
    const held = CASES[2];
    renderVi(<ItemReviewCard project={PROJECT} slug="demo-shop" version={3} state="in_review" useCase={held} />);
    const card = screen.getByTestId("srs-item-review");
    expect(card).toHaveAttribute("data-state", "changes_requested");
    expect(card).toHaveTextContent("Có người yêu cầu sửa");
    expect(within(card).getByText("bao")).toBeInTheDocument();
    expect(within(card).getByRole("link", { name: "Mở trang duyệt v3" })).toHaveAttribute("href", "/p/demo/x/srs/documents/demo-shop/versions/3");
  });

  it("tells a draft's use case that it is reviewed once the version is submitted", () => {
    renderVi(<ItemReviewCard project={PROJECT} slug="demo-shop" version={4} state="draft" useCase={CASES[0]} />);
    expect(screen.getByTestId("srs-item-review")).toHaveTextContent("v4 đang là bản nháp");
    expect(screen.queryByRole("link", { name: /Mở trang duyệt/ })).toBeNull();
  });
});
