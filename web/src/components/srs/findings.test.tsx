// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { documentOf, finding } from "@/test/fixtures/srs";
import { renderVi } from "@/test/render";

import { srsKeys } from "./api";
import { FindingsCard, type FindingsRead } from "./findings-card";
import { approveLock, chatTarget, groupFindings, reviewHolds, settlerOf } from "./findings-model";
import { ReviewForm } from "./review-form";

const PROJECT = "demo";
const CHAT = "/chats/12?message=40";

/** jsdom has no ResizeObserver; Radix's radio group measures its items with one. */
class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

beforeEach(() => vi.stubGlobal("ResizeObserver", ResizeObserverStub));
afterEach(() => vi.unstubAllGlobals());

function withClient(ui: ReactElement, client = new QueryClient({ defaultOptions: { queries: { staleTime: Infinity, retry: false } } })) {
  return { client, ...renderVi(<QueryClientProvider client={client}>{ui}</QueryClientProvider>) };
}

function read(data: FindingsRead["data"], patch: Partial<FindingsRead> = {}): FindingsRead {
  return { data, isPending: data === undefined, isError: false, refetch: vi.fn(), ...patch };
}

const SETTLED = finding(3, { status: "resolved", settled_by: "bao", settled_at: "2026-10-12T09:00:00Z", resolution: "Three retries, as the plan says.", chat_link: CHAT });
const FROM_GRAPH = finding(4, {
  level: "advisory",
  status: "resolved",
  settled_at: "2026-10-12T10:00:00Z",
  resolution: "The plan decided it.",
  resolution_sources: [{ kind: "node", ref: "plan:rollout/step:1", note: null, document: null }],
});
const WAIVED = finding(5, { level: "info", kind: "overlap", status: "waived", settled_at: "2026-10-12T08:00:00Z", resolution: "Not a conflict." });
const ADVICE = finding(2, { level: "advisory", kind: "duplicate", item: null, item_code: null, sources: [{ kind: "path", ref: "lumen-docs:docs/billing.md", note: null, document: null }] });
const DECIDE = finding(6, { chat_link: CHAT, sources: [{ kind: "code", ref: "UC NW-1", note: null, document: null }] });

describe("the findings model", () => {
  it("lists the open findings to decide first, then the other open ones by level, then the settled ones, last settled first", () => {
    const groups = groupFindings([SETTLED, FROM_GRAPH, WAIVED, ADVICE, finding(1), DECIDE, finding(7, { level: "info" })]);
    expect(groups.decide.map((one) => one.id)).toEqual([1, 6]);
    expect(groups.open.map((one) => one.id)).toEqual([2, 7]);
    expect(groups.settled.map((one) => one.id)).toEqual([4, 3, 5]);
  });

  it("locks Approve as the hub's gate would: findings to decide first, with their chat, then @ba's review while it holds", () => {
    const running = { id: 431, state: "running" } as const;
    expect(approveLock([finding(1), DECIDE, SETTLED], running)).toEqual({ kind: "findings", count: 2, chat: CHAT });
    expect(approveLock([finding(1, { chat_link: "javascript:alert(1)" })], null)).toEqual({ kind: "findings", count: 1, chat: null });
    expect(approveLock([ADVICE, SETTLED], running)).toEqual({ kind: "review", run: running });
    expect(approveLock(undefined, { id: 431, state: "queued" })).toEqual({ kind: "review", run: { id: 431, state: "queued" } });
    for (const state of ["done", "failed", "cancelled", "lost"] as const) expect(approveLock([ADVICE], { id: 431, state })).toBeNull();
    expect(approveLock(undefined, null)).toBeNull();
    expect(reviewHolds({ id: 1, state: "waiting" })).toBe(true);
  });

  it("takes a chat link only as a path of the hub or an http(s) URL", () => {
    expect(chatTarget(CHAT)).toEqual({ href: CHAT, external: false });
    expect(chatTarget("https://chat.example.org/m/4")).toEqual({ href: "https://chat.example.org/m/4", external: true });
    for (const link of ["//evil.example.org/x", "/\\evil.example.org", "javascript:alert(1)", "chats/12", "", null]) expect(chatTarget(link)).toBeNull();
  });

  it("names who settled a finding: a member, the knowledge graph, or @ba's waiver", () => {
    expect(settlerOf(SETTLED)).toEqual({ by: "member", login: "bao" });
    expect(settlerOf(FROM_GRAPH)).toEqual({ by: "graph" });
    expect(settlerOf(WAIVED)).toEqual({ by: "agent" });
    expect(settlerOf(DECIDE)).toBeNull();
  });
});

describe("FindingsCard", () => {
  it("draws the open findings to decide first with their count, each source linked to its item, and how the settled ones were settled", () => {
    withClient(<FindingsCard project={PROJECT} slug="demo-shop" round={1} run={null} inReview findings={read([SETTLED, FROM_GRAPH, WAIVED, ADVICE, DECIDE, finding(1)])} />);
    const card = screen.getByTestId("srs-findings");
    expect(within(card).getByTestId("srs-findings-counts")).toHaveTextContent("2 cần quyết định, 1 mở khác, 3 đã chốt");
    const sections = within(card).getAllByRole("region");
    expect(sections.map((section) => section.dataset.testid)).toEqual(["srs-findings-decide", "srs-findings-open", "srs-findings-settled"]);
    expect(within(sections[0]).getByRole("heading", { level: 3 })).toHaveTextContent("Cần quyết định2");
    const first = within(sections[0]).getAllByTestId("srs-finding")[0];
    expect(first).toHaveAttribute("data-finding", "1");
    expect(within(first).getByTestId("srs-finding-level")).toHaveTextContent("Cần quyết định");
    expect(within(first).getByTestId("srs-finding-kind")).toHaveTextContent("Mâu thuẫn");
    expect(within(first).getByTestId("srs-finding-status")).toHaveTextContent("Đang mở");
    expect(within(first).getByTestId("srs-finding-summary")).toHaveTextContent("Demo-shop retries a payment 3 times, billing 5.");
    expect(within(first).getByRole("link", { name: "BR ORD1-1" })).toHaveAttribute("href", "/p/demo/x/srs/documents/demo-shop/items/BR%20ORD1-1?v=3");
    expect(within(first).getByRole("link", { name: "BR BIL2-1" })).toHaveAttribute("href", "/p/demo/x/srs/documents/billing/items/BR%20BIL2-1");
    expect(within(first).getByText("billing retries 5 times")).toBeInTheDocument();
    expect(within(first).queryByTestId("srs-finding-chat")).toBeNull();
    // a code whose document the reader does not see is no link; an open finding links its settle chat
    const second = within(sections[0]).getAllByTestId("srs-finding")[1];
    expect(within(second).queryByRole("link", { name: "UC NW-1" })).toBeNull();
    expect(within(second).getByText("UC NW-1")).toBeInTheDocument();
    expect(within(second).getByTestId("srs-finding-chat")).toHaveAttribute("href", CHAT);
    // the whole document, and a file
    const advice = within(sections[1]).getByTestId("srs-finding");
    expect(advice).toHaveTextContent("Cả tài liệu");
    expect(within(advice).getByText("lumen-docs:docs/billing.md")).toBeInTheDocument();
    // settled: by whom, the words, what settled it, the message
    const [graph, member, waived] = within(sections[2]).getAllByTestId("srs-finding");
    expect(within(graph).getByTestId("srs-finding-settler")).toHaveTextContent("Đã chốt từ knowledge graph");
    expect(within(graph).getByRole("link", { name: "plan:rollout/step:1" })).toHaveAttribute("href", "/p/demo/kg/node?id=plan%3Arollout%2Fstep%3A1");
    expect(within(member).getByTestId("srs-finding-settler")).toHaveTextContent("bao đã chốt");
    expect(within(member).getByText("Three retries, as the plan says.")).toBeInTheDocument();
    expect(within(member).getByTestId("srs-finding-chat")).toHaveAttribute("href", CHAT);
    expect(within(waived).getByTestId("srs-finding-settler")).toHaveTextContent("@ba đã bỏ qua");
    expect(within(waived).getByTestId("srs-finding-status")).toHaveAttribute("data-status", "waived");
    expect(within(card).queryByRole("button")).toBeNull();
  });

  it("says @ba is reviewing the round, a review that did not finish, nothing found, loading and a failed read with a retry", async () => {
    const user = userEvent.setup();
    const { unmount } = withClient(<FindingsCard project={PROJECT} slug="demo-shop" round={2} run={{ id: 431, state: "running" }} inReview findings={read([])} />);
    const reviewing = screen.getByTestId("srs-findings-reviewing");
    expect(reviewing).toHaveTextContent("@ba đang rà soát vòng 2");
    expect(within(reviewing).getByRole("link", { name: "Mở run #431" })).toHaveAttribute("href", "/p/demo/runs/431");
    expect(screen.getByTestId("srs-findings-empty")).toHaveTextContent("@ba chưa ghi phát hiện nào.");
    unmount();

    const broke = withClient(<FindingsCard project={PROJECT} slug="demo-shop" round={2} run={{ id: 431, state: "failed" }} inReview findings={read(undefined)} />);
    expect(screen.getByTestId("srs-findings-review-broke")).toHaveTextContent("@ba chưa rà soát xong vòng 2");
    expect(screen.getByTestId("state-loading")).toBeInTheDocument();
    broke.unmount();

    const refetch = vi.fn();
    withClient(<FindingsCard project={PROJECT} slug="demo-shop" round={1} run={{ id: 431, state: "done" }} inReview findings={read(undefined, { isPending: false, isError: true, refetch })} />);
    expect(screen.queryByTestId("srs-findings-reviewing")).toBeNull();
    expect(screen.getByRole("alert")).toHaveTextContent("Không đọc được các phát hiện.");
    await user.click(screen.getByRole("button", { name: "Thử lại" }));
    expect(refetch).toHaveBeenCalledOnce();
  });
});

describe("ReviewForm with a lock", () => {
  const version = documentOf().versions[0];

  it("disables Approve with the findings that hold it and their chat, and takes Request changes", () => {
    withClient(<ReviewForm project={PROJECT} slug="demo-shop" version={version} roles={[]} lock={{ kind: "findings", count: 2, chat: CHAT }} />);
    const approve = screen.getByTestId("srs-review-decision-approve");
    expect(approve).toBeDisabled();
    expect(screen.getByTestId("srs-review-decision-request_changes")).toHaveAttribute("aria-checked", "true");
    const lock = screen.getByTestId("srs-review-lock");
    expect(lock).toHaveTextContent("Chưa duyệt được: còn 2 phát hiện cần quyết định đang mở");
    expect(within(lock).getByRole("link", { name: "Mở chat chốt" })).toHaveAttribute("href", CHAT);
    expect(approve.getAttribute("aria-describedby")).toContain(lock.parentElement!.id);
    expect(screen.getByText("Ghi chú (bắt buộc khi yêu cầu sửa)")).toBeInTheDocument();
  });

  it("names @ba's review run that holds Approve", () => {
    withClient(<ReviewForm project={PROJECT} slug="demo-shop" version={version} roles={[]} lock={{ kind: "review", run: { id: 431, state: "queued" } }} />);
    expect(screen.getByTestId("srs-review-decision-approve")).toBeDisabled();
    const lock = screen.getByTestId("srs-review-lock");
    expect(lock).toHaveTextContent("Duyệt chờ @ba rà soát xong vòng 1");
    expect(within(lock).getByRole("link", { name: "Mở run #431" })).toHaveAttribute("href", "/p/demo/runs/431");
    expect(within(lock).getByTestId("srs-review-run")).toHaveAttribute("data-state", "queued");
  });

  it("shows the hub's 409 as a refusal and reads the document and its findings again", async () => {
    const user = userEvent.setup();
    vi.stubGlobal(
      "fetch",
      vi.fn(async (request: Request) => {
        const { pathname } = new URL(request.url);
        if (pathname === "/v1/auth/web/csrf") return json({ csrf: "token", header: "X-Evo-CSRF" });
        if (request.method === "POST" && pathname.endsWith("/reviews")) {
          return json({ code: "conflict", message: "version 3 of demo-shop is not approved while 1 finding(s) of level decide are open", detail: [] }, 409);
        }
        return json({ code: "not_found", message: "no API in unit tests" }, 404);
      }),
    );
    const { client } = withClient(<ReviewForm project={PROJECT} slug="demo-shop" version={version} roles={[]} />);
    const invalidate = vi.spyOn(client, "invalidateQueries");
    await user.click(screen.getByTestId("srs-review-submit"));
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    await waitFor(() => expect(invalidate).toHaveBeenCalledWith({ queryKey: srsKeys.document(PROJECT, "demo-shop") }));
    expect(srsKeys.findings(PROJECT, "demo-shop").slice(0, srsKeys.document(PROJECT, "demo-shop").length)).toEqual([...srsKeys.document(PROJECT, "demo-shop")]);
  });
});
