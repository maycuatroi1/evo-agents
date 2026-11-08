// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { createApiClient } from "@/lib/api/client";
import { pluginKeys } from "@/lib/plugin-queries";
import { queryKeys } from "@/lib/queries";
import { aQueuedCase, aUseCase, documentOf, finding, ITEMS, queued, version } from "@/test/fixtures/srs";
import { renderVi } from "@/test/render";

import type { SrsDocument, SrsFinding, SrsItem, SrsQueuedVersion } from "./api";
import { srsKeys } from "./api";
import { CaseReviewPage } from "./case-review-page";

/** The hub as the page sees it: what it answers each read with, and every request it was sent. */
const hub = vi.hoisted(() => ({
  seen: [] as Request[],
  latest: null as unknown,
  approved: null as unknown,
  findings: [] as unknown[],
  queue: [] as unknown[],
  write: null as null | ((request: Request) => Response),
}));

vi.mock("@/lib/api/browser", () => ({
  browserApi: () =>
    createApiClient({
      baseUrl: "http://hub.test",
      fetch: async (request) => {
        hub.seen.push(request);
        const url = new URL(request.url);
        if (url.pathname === "/v1/auth/web/csrf") return Response.json({ csrf: "csrf-1", header: "X-Evo-CSRF" });
        if (request.method !== "GET") return hub.write ? hub.write(request) : Response.json({}, { status: 500 });
        if (url.pathname.endsWith("/findings")) return Response.json(hub.findings);
        if (url.pathname.endsWith("/reviews")) return Response.json(hub.queue);
        if (url.pathname.endsWith("/documents/demo-shop")) return Response.json(url.searchParams.get("version") === "2" ? hub.approved : hub.latest);
        return Response.json({ fields: [] });
      },
    }),
}));

const PROJECT = "demo";
const before = ITEMS.find((one) => one.key === "uc1")!;
/** UC ORD-1 as v3 reads it: the actor reworded, its goal sharpened, a context written. */
const after: SrsItem = {
  ...before,
  fields: { ...before.fields, objective: undefined, intent: "The customer turns a full cart into an order", actor: "Signed-in customer", context: "The shop is open" },
};

/** demo-shop with v3 in review by alice and v2 approved on 2026-10-10. */
function documents(): { latest: SrsDocument; approved: SrsDocument } {
  const doc = documentOf();
  const versions = [doc.versions[0], version(2, { state: "approved", round: 1, author: "chi", decided_at: "2026-10-10T09:00:00Z" }), doc.versions[2]];
  const latest = { ...doc, versions, shown: { ...doc.shown!, items: ITEMS.map((one) => (one.key === "uc1" ? after : one)), use_cases: [aUseCase()] } };
  return { latest, approved: { ...latest, shown: { ...latest.shown, ...versions[1], items: ITEMS, use_cases: [] } } };
}

const DECIDE = finding(7, {
  summary: "Carts hold 50 lines here, billing caps an order at 40. Which wins?",
  options: ["Keep 50 and change billing", "Use 40 here"],
});

function show({ findings = [DECIDE], queue = [] as SrsQueuedVersion[], role = "writer" as "writer" | "reader", login = "bao", run = null as SrsDocument["versions"][number]["review_run"] } = {}) {
  const { latest, approved } = documents();
  latest.versions[0] = { ...latest.versions[0], review_run: run };
  hub.latest = latest;
  hub.approved = approved;
  hub.findings = findings;
  hub.queue = queue;
  const client = new QueryClient({ defaultOptions: { queries: { staleTime: Infinity, retry: false } } });
  client.setQueryData(srsKeys.version(PROJECT, "demo-shop", null), latest);
  client.setQueryData(srsKeys.version(PROJECT, "demo-shop", 2), approved);
  client.setQueryData(srsKeys.openFindings(PROJECT, "demo-shop"), findings as SrsFinding[]);
  client.setQueryData(srsKeys.reviews(PROJECT), queue);
  client.setQueryData(pluginKeys.effective(PROJECT, "srs"), { plugin: "srs", version: "1.0.0", project: PROJECT, agent: "default", fields: [], problems: [], digest: "d" });
  client.setQueryData(queryKeys.whoami, {
    login,
    admin: false,
    token: { kind: "web", scopes: [], expires_at: null },
    grants: [{ project: PROJECT, role, max_level: "internal" }],
  });
  return renderVi(
    <QueryClientProvider client={client}>
      <CaseReviewPage project={PROJECT} slug="demo-shop" number={3} caseKey="uc1" initialError={null} />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  hub.seen = [];
  hub.write = null;
});

describe("CaseReviewPage", () => {
  it("says who asks for what, the four steps, and what the use case says with this change marked", () => {
    show();
    expect(screen.getByTestId("srs-case-back")).toHaveAttribute("href", "/p/demo/x/srs/reviews");
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Place an order");
    const sub = screen.getByTestId("srs-case-head");
    expect(sub).toHaveTextContent("UC ORD-1. alice nhờ bạn duyệt bản sửa ngày");
    expect(sub).toHaveTextContent("Bản đang dùng là bản duyệt ngày");
    const steps = within(screen.getByTestId("srs-case-steps")).getAllByRole("link");
    expect(steps.map((link) => link.textContent)).toEqual(["1. Use case nói gì", "2. Lần này sửa gì", "3. @ba lưu ý", "4. Quyết định"]);

    const content = screen.getByTestId("srs-case-content");
    const fields = within(content).getAllByTestId("srs-case-field");
    expect(fields.map((field) => [field.getAttribute("data-field"), field.getAttribute("data-change")])).toEqual([
      ["actor", "changed"],
      ["intent", "changed"],
      ["context", "added"],
    ]);
    expect(within(content).getByText("Ai dùng")).toBeInTheDocument();
    expect(within(content).getByText("Muốn đạt gì")).toBeInTheDocument();
    // The goal keeps its words and marks the one added; the actor, rewritten, is replaced whole.
    expect(fields[1].querySelector("ins")).toHaveTextContent("thêm: full");
    expect(fields[1].querySelector("del")).toBeNull();
    expect(fields[0].querySelector("del")).toHaveTextContent("bỏ: Customer");
    expect(within(content).getByTestId("srs-case-more")).toHaveTextContent("Các bước (2) và quy tắc (1)");

    const changes = screen.getByTestId("srs-case-changes");
    expect(within(changes).getByTestId("srs-case-summary")).toHaveTextContent("Version 3");
    expect(within(changes).getAllByTestId("srs-case-change").map((one) => one.textContent)).toEqual(["Đã sửaAi dùng", "Đã sửaMuốn đạt gì", "MớiBối cảnh"]);
    expect(within(changes).getByTestId("srs-case-compare")).toHaveAttribute("href", "/p/demo/x/srs/documents/demo-shop/compare?from=2&to=3");
  });

  it("asks the point to decide as a question with its choices, and settles it with the one picked", async () => {
    const user = userEvent.setup();
    hub.write = () => Response.json({ ...DECIDE, status: "resolved" });
    show();
    const ba = screen.getByTestId("srs-case-ba");
    expect(within(ba).getByTestId("srs-case-question")).toHaveTextContent("Which wins?");
    expect(within(ba).getByText("Về")).toBeInTheDocument();
    const options = within(ba).getAllByTestId("srs-case-option");
    expect(options.map((one) => one.textContent)).toEqual(["A.Keep 50 and change billing", "B.Use 40 here"]);
    // Agree waits for it, and says so in one line.
    expect(screen.getByTestId("srs-case-approve")).toBeDisabled();
    expect(screen.getByTestId("srs-case-gate")).toHaveTextContent("Chọn xong 1 điểm ở bước 3 thì nút Đồng ý mở.");

    await user.click(options[1]);
    expect(options[1]).toHaveAttribute("aria-pressed", "true");
    await user.type(within(ba).getByTestId("srs-case-settle-note"), "Billing is the contract");
    await user.click(within(ba).getByTestId("srs-case-settle-send"));
    await waitFor(() => expect(hub.seen.some((request) => request.method === "POST")).toBe(true));
    const post = hub.seen.find((request) => request.method === "POST")!;
    expect(post.url).toBe("http://hub.test/v1/x/srs/demo/documents/demo-shop/findings/7/settle");
    expect(post.headers.get("X-Evo-CSRF")).toBe("csrf-1");
    expect(await post.clone().json()).toEqual({ option: 1, note: "Billing is the contract" });
  });

  it("settles a point with no choice by a note, which it asks for first", async () => {
    const user = userEvent.setup();
    hub.write = () => Response.json({ ...DECIDE, status: "resolved" });
    show({ findings: [finding(8, { item: "uc1" })] });
    await user.click(screen.getByTestId("srs-case-settle-send"));
    expect(screen.getByRole("alert")).toHaveTextContent("Hãy ghi cách chốt.");
    expect(hub.seen.filter((request) => request.method === "POST")).toHaveLength(0);
    await user.type(screen.getByTestId("srs-case-settle-note"), "Billing decides");
    await user.click(screen.getByTestId("srs-case-settle-send"));
    await waitFor(() => expect(hub.seen.some((request) => request.method === "POST")).toBe(true));
    expect(await hub.seen.find((request) => request.method === "POST")!.clone().json()).toEqual({ note: "Billing decides" });
  });

  it("folds the advice and the points elsewhere, and says when @ba sees nothing", () => {
    const { unmount } = show({ findings: [finding(4, { item: "nfr1" }), finding(5, { item: "uc1", level: "advisory" })], run: { id: 40, state: "done" } });
    expect(screen.getByTestId("srs-case-elsewhere")).toHaveTextContent("1 điểm cần chốt ở phần khác của tài liệu");
    expect(screen.getByTestId("srs-case-advice-list")).toHaveTextContent("Gợi ý thêm (1)");
    expect(screen.getByTestId("srs-case-gate")).toHaveTextContent("Còn 1 điểm ở phần khác của tài liệu cần chốt");
    unmount();

    show({ findings: [], run: { id: 40, state: "done" } });
    expect(screen.getByTestId("srs-case-ba-none")).toHaveTextContent("@ba không thấy điểm nào cần chốt.");
    expect(screen.getByTestId("srs-case-approve")).toBeEnabled();
  });

  it("says @ba is still checking, and keeps Agree closed until it is done", () => {
    show({ findings: [], run: { id: 431, state: "running" } });
    expect(screen.getByTestId("srs-case-ba-reviewing")).toHaveTextContent("@ba đang kiểm tra use case này, thường mất khoảng 5 phút.");
    expect(screen.getByTestId("srs-case-ba-reviewing")).toHaveTextContent("Bạn có thể đọc trước.");
    expect(screen.getByTestId("srs-case-approve")).toBeDisabled();
    expect(screen.getByTestId("srs-case-gate")).toHaveTextContent("@ba đang kiểm tra, nút Đồng ý mở khi xong.");
  });

  it("agrees, then offers the next use case waiting for the reviewer", async () => {
    const user = userEvent.setup();
    hub.write = () => Response.json({ ...documents().latest.versions[0], use_cases: [] });
    const next = queued({ document: "northwind-billing", number: 5, use_cases: [aQueuedCase({ key: "b1", code: "UC BIL-1" })] });
    show({ findings: [], queue: [queued(), next] });
    await user.click(screen.getByTestId("srs-case-approve"));
    await waitFor(() => expect(screen.getByTestId("srs-case-done")).toHaveTextContent("Bạn đã đồng ý use case này."));
    const post = hub.seen.find((request) => request.method === "POST")!;
    expect(post.url).toBe("http://hub.test/v1/x/srs/demo/documents/demo-shop/versions/3/items/UC%20ORD-1/reviews");
    expect(await post.clone().json()).toEqual({ decision: "approve", role: null, body: "" });
    expect(screen.getByTestId("srs-case-next")).toHaveAttribute("href", "/p/demo/x/srs/documents/northwind-billing/versions/5/cases/b1");
    expect(screen.getByTestId("srs-case-list")).toHaveAttribute("href", "/p/demo/x/srs/reviews");
  });

  it("asks what needs changing before it sends a request for changes", async () => {
    const user = userEvent.setup();
    hub.write = () => Response.json({ ...documents().latest.versions[0], use_cases: [] });
    show({ findings: [] });
    await user.click(screen.getByTestId("srs-case-request"));
    expect(screen.getByLabelText("Cần sửa gì?")).toHaveFocus();
    await user.click(screen.getByTestId("srs-case-send"));
    expect(screen.getByRole("alert")).toHaveTextContent("Hãy ghi cần sửa gì.");
    await user.type(screen.getByTestId("srs-case-note"), "Say who pays");
    await user.click(screen.getByTestId("srs-case-send"));
    await waitFor(() => expect(screen.getByTestId("srs-case-done")).toHaveTextContent("Bạn đã yêu cầu sửa. alice sẽ nhận ghi chú của bạn."));
    expect(screen.getByTestId("srs-case-list")).toHaveAttribute("href", "/p/demo/x/srs/reviews");
    expect(screen.queryByTestId("srs-case-next")).toBeNull();
  });

  it("tells the author and a reader plainly why they cannot decide", () => {
    const { unmount } = show({ login: "alice" });
    expect(screen.getByTestId("srs-case-blocked")).toHaveTextContent("Bạn viết bản sửa này nên không tự duyệt được");
    expect(screen.queryByTestId("srs-case-approve")).toBeNull();
    expect(screen.getByTestId("srs-case-head")).toHaveTextContent("alice gửi bản sửa ngày");
    unmount();

    show({ role: "reader" });
    expect(screen.getByTestId("srs-case-blocked")).toHaveTextContent("Bạn chỉ có quyền đọc trong dự án này");
    // A reader sees the choices, with nothing to press.
    expect(screen.queryByTestId("srs-case-option")).toBeNull();
    expect(screen.getByRole("list", { name: "Các hướng" })).toHaveTextContent("A. Keep 50 and change billing");
  });
});
