// @vitest-environment jsdom
import { QueryClientProvider } from "@tanstack/react-query";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { createApiClient } from "@/lib/api/client";
import { kgKeys } from "@/lib/kg/queries";
import type { ReviewFilter } from "@/lib/kg/routes";
import type { ReviewItem, ReviewQueue } from "@/lib/kg/types";
import { makeQueryClient } from "@/lib/query-client";
import { renderVi } from "@/test/render";

import { KgReviewPage } from "./kg-review-page";

const api = vi.hoisted(() => ({
  answer: null as null | ((request: Request) => Response | Promise<Response>),
  seen: [] as Request[],
}));

vi.mock("@/lib/api/browser", () => ({
  browserApi: () =>
    createApiClient({
      baseUrl: "http://hub.test",
      fetch: async (request) => {
        api.seen.push(request);
        if (request.url.endsWith("/v1/auth/web/csrf")) return Response.json({ csrf: "csrf-1", header: "X-Evo-CSRF" });
        if (!api.answer) throw new Error("no answer set");
        return api.answer(request);
      },
    }),
}));

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn() }),
  usePathname: () => "/p/demo/kg/review",
  useSearchParams: () => new URLSearchParams(),
}));

const graph = { build_id: 7, content_hash: "sha256:" + "a".repeat(64), nodes: 5, edges: 4, finished_at: "2026-10-04T10:00:00Z", latest: true };
const OPEN: Required<ReviewFilter> = { rule: null, cluster: null, state: "open" };

const item = (fingerprint: string, extra: Partial<ReviewItem> = {}): ReviewItem => ({
  fingerprint,
  rule: "unmentioned_document",
  action: "link_or_deprecate",
  cluster: "c1",
  cluster_label: "runbook, cài đặt",
  nodes: [{ id: `docs:doc:${fingerprint}`, kind: "Document", name: `note ${fingerprint}` }],
  detail: { count: 1, path: null },
  weight: 2,
  dismissal: null,
  ...extra,
});

const counts = (open: Partial<Record<ReviewItem["rule"], number>>) =>
  (["dangling_mention", "unmentioned_document", "cluster_without_knowledge", "possible_duplicate", "ambiguous_match", "bulk_noise"] as const).map((rule) => ({
    rule,
    open: open[rule] ?? 0,
    dismissed: 0,
  }));

const QUEUE: ReviewQueue = {
  graph,
  computed: true,
  reason: null,
  can_dismiss: true,
  counts: counts({ unmentioned_document: 2, dangling_mention: 1 }),
  items: [
    item("r:00000000000000a1", { rule: "dangling_mention", action: "fix_mention", detail: { count: 1, mentions: 3, samples: ["gone.py"] } }),
    item("r:00000000000000b2"),
    item("r:00000000000000c3", { cluster: null, cluster_label: null, weight: 0 }),
  ],
};

function withQueries(ui: ReactElement, queue: ReviewQueue, filter = OPEN) {
  const client = makeQueryClient();
  client.setQueryData(kgKeys.reviewList("demo", filter), queue);
  return { client, ...renderVi(<QueryClientProvider client={client}>{ui}</QueryClientProvider>) };
}

beforeEach(() => {
  api.answer = null;
  api.seen = [];
});

describe("KgReviewPage", () => {
  it("groups the items by rule then cluster, with links to their nodes and clusters", () => {
    withQueries(<KgReviewPage project="demo" filter={OPEN} error={null} />, QUEUE);
    const dangling = screen.getByTestId("kg-review-group-dangling_mention");
    expect(dangling).toHaveTextContent("Nhắc tới mã không còn nữa");
    expect(dangling).toHaveTextContent("3 lần nhắc");
    expect(dangling).toHaveTextContent("gone.py");
    const documents = screen.getByTestId("kg-review-group-unmentioned_document");
    const clusters = within(documents).getAllByTestId("kg-review-cluster");
    expect(clusters).toHaveLength(2);
    expect(within(clusters[0]!).getByRole("link", { name: "runbook, cài đặt" })).toHaveAttribute("href", "/p/demo/kg/cluster?id=c1");
    expect(clusters[1]).toHaveTextContent("Không thuộc cụm nào");
    expect(within(documents).getAllByTestId("kg-review-node")[0]).toHaveAttribute("href", "/p/demo/kg/node?id=docs%3Adoc%3Ar%3A00000000000000b2");
    expect(screen.getByTestId("kg-review-rule-unmentioned_document")).toHaveAttribute("href", "/p/demo/kg/review?rule=unmentioned_document");
    expect(screen.getByTestId("kg-review-state-all")).toHaveAttribute("href", "/p/demo/kg/review?state=all");
    expect(screen.queryByTestId("kg-review-group-bulk_noise")).toBeNull();
  });

  it("lets a writer dismiss an item with a reason and asks the queue again", async () => {
    const { client } = withQueries(<KgReviewPage project="demo" filter={OPEN} error={null} />, QUEUE);
    api.answer = (request) =>
      request.method === "POST"
        ? Response.json({ reason: "kept on purpose", by: "wendy", at: "2026-10-25T08:00:00Z", build_id: 7 })
        : Response.json(QUEUE);
    const target = screen.getAllByTestId("kg-review-item").find((li) => li.dataset.fingerprint === "r:00000000000000b2")!;
    await userEvent.click(within(target).getByTestId("kg-review-dismiss"));
    const save = within(target).getByTestId("kg-review-dismiss-save");
    expect(save).toBeDisabled();
    await userEvent.type(within(target).getByTestId("kg-review-reason"), "  kept on purpose ");
    await userEvent.click(save);
    await waitFor(() => expect(api.seen.some((r) => r.method === "POST")).toBe(true));
    const post = api.seen.find((r) => r.method === "POST")!;
    expect(new URL(post.url).pathname).toBe("/v1/kg/demo/review/r%3A00000000000000b2/dismiss");
    expect(await post.json()).toEqual({ reason: "kept on purpose" });
    expect(post.headers.get("X-Evo-CSRF")).toBe("csrf-1");
    // the queue is asked again once the hub has the dismissal
    await waitFor(() => expect(api.seen.some((r) => r.method === "GET" && new URL(r.url).pathname === "/v1/kg/demo/review")).toBe(true));
    expect(client.getQueryData(kgKeys.reviewList("demo", OPEN))).toEqual(QUEUE);
  });

  it("shows a refusal of the hub in the form", async () => {
    withQueries(<KgReviewPage project="demo" filter={OPEN} error={null} />, QUEUE);
    api.answer = () => Response.json({ error: { code: "not_found", message: "gone" } }, { status: 404 });
    const target = screen.getAllByTestId("kg-review-item")[0]!;
    await userEvent.click(within(target).getByTestId("kg-review-dismiss"));
    await userEvent.type(within(target).getByTestId("kg-review-reason"), "nothing to do");
    await userEvent.click(within(target).getByTestId("kg-review-dismiss-save"));
    expect(await within(target).findByText("Mục đó không còn trong hàng đợi bạn thấy.")).toBeInTheDocument();
  });

  it("gives a reader no dismiss button and shows who dismissed an item", () => {
    const dismissed = item("r:00000000000000d4", { dismissal: { reason: "an archive", by: "wendy", at: "2026-10-20T08:00:00Z", build_id: 6 } });
    const all: Required<ReviewFilter> = { rule: null, cluster: null, state: "all" };
    withQueries(<KgReviewPage project="demo" filter={all} error={null} />, { ...QUEUE, can_dismiss: false, items: [dismissed] }, all);
    expect(screen.queryByTestId("kg-review-dismiss")).toBeNull();
    expect(screen.getByTestId("kg-review-dismissal")).toHaveTextContent("wendy đã bỏ qua: an archive");
    expect(screen.getByText("Bạn đọc được hàng đợi; writer của dự án mới bỏ qua được mục.")).toBeInTheDocument();
  });

  it("says why there is no queue, and when no item matches", () => {
    const { unmount } = withQueries(
      <KgReviewPage project="demo" filter={OPEN} error={null} />,
      { ...QUEUE, computed: false, reason: "this build has no review queue: it has no clusters", items: [] },
    );
    expect(screen.getByTestId("kg-review-none")).toHaveTextContent("it has no clusters");
    unmount();
    withQueries(<KgReviewPage project="demo" filter={OPEN} error={null} />, { ...QUEUE, items: [] });
    expect(screen.getByTestId("kg-review-empty")).toHaveTextContent("Không có mục nào khớp");
  });
});
