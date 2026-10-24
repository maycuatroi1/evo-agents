// @vitest-environment jsdom
import { QueryClientProvider } from "@tanstack/react-query";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { createApiClient } from "@/lib/api/client";
import { kgKeys } from "@/lib/kg/queries";
import type { KgDecision, KgDecisionList } from "@/lib/kg/types";
import { makeQueryClient } from "@/lib/query-client";
import { renderVi } from "@/test/render";

import { ClusterDecisions, NodeDecisions, ReviewDecisions } from "./decisions";

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

const NODE = "app:file:app/search.py";
const graph = { build_id: 7, content_hash: "sha256:" + "a".repeat(64), nodes: 5, edges: 4, finished_at: "2026-10-04T10:00:00Z", latest: true };

const decision = (number: number, extra: Partial<KgDecision> = {}): KgDecision => ({
  number,
  kind: "deprecate",
  state: "active",
  src: NODE,
  rel: null,
  dst: null,
  history: [{ rev: number, action: "decide", by: "wendy", at: "2026-10-25T08:00:00Z", reason: "ranking moved", build_id: 7 }],
  ...extra,
});

const LIST: KgDecisionList = {
  graph,
  rev: 4,
  can_decide: true,
  decisions: [
    decision(4, { kind: "link", state: "proposed", rel: "verifies", dst: "app:file:tests/test_search.py", history: [{ rev: 4, action: "propose", by: "agent:kg", at: "2026-10-25T09:00:00Z", reason: "the test covers it", build_id: 7 }] }),
    decision(3, { src: "app:file:gone.py", state: "orphan" }),
    decision(2, { src: "app:file:other.py" }),
    decision(1),
  ],
};

function withList(ui: ReactElement, list: KgDecisionList = LIST) {
  const client = makeQueryClient();
  client.setQueryData(kgKeys.decisions("demo"), list);
  return { client, ...renderVi(<QueryClientProvider client={client}>{ui}</QueryClientProvider>) };
}

async function posted() {
  await waitFor(() => expect(api.seen.some((r) => r.method === "POST")).toBe(true));
  return api.seen.find((r) => r.method === "POST")!;
}

beforeEach(() => {
  api.answer = (request) => (request.method === "POST" ? Response.json(decision(5), { status: 201 }) : Response.json(LIST));
  api.seen = [];
});

describe("kg decisions", () => {
  it("shows the decisions about a node, and a writer deprecates it with a reason and the revision read", async () => {
    withList(<NodeDecisions project="demo" nodeId={NODE} />);
    const list = screen.getByTestId("kg-decision-list");
    expect(within(list).getAllByTestId("kg-decision").map((li) => li.dataset.number)).toEqual(["4", "1"]);
    expect(list).toHaveTextContent("app:file:app/search.py -verifies-> app:file:tests/test_search.py");
    expect(list).toHaveTextContent("agent:kg đã đề xuất quyết định 4");
    const take = screen.getByTestId("kg-decision-take");
    expect(take).toBeDisabled();
    await userEvent.type(screen.getByTestId("kg-decision-reason"), " ranking moved ");
    await userEvent.click(take);
    const post = await posted();
    expect(new URL(post.url).pathname).toBe("/v1/kg/demo/decisions");
    expect(await post.json()).toEqual({ kind: "deprecate", src: NODE, rel: null, dst: null, reason: "ranking moved", base_rev: 4 });
    expect(post.headers.get("X-Evo-CSRF")).toBe("csrf-1");
  });

  it("a link decision needs a relation and the other node", async () => {
    withList(<NodeDecisions project="demo" nodeId={NODE} />);
    await userEvent.selectOptions(screen.getByTestId("kg-decision-kind"), "link");
    await userEvent.type(screen.getByTestId("kg-decision-reason"), "documented there");
    await userEvent.type(screen.getByTestId("kg-decision-to"), "docs:file:guide.md");
    expect(screen.getByTestId("kg-decision-take")).toBeDisabled();
    await userEvent.type(screen.getByTestId("kg-decision-rel"), "documents");
    await userEvent.click(screen.getByTestId("kg-decision-take"));
    expect(await (await posted()).json()).toEqual({
      kind: "link",
      src: NODE,
      rel: "documents",
      dst: "docs:file:guide.md",
      reason: "documented there",
      base_rev: 4,
    });
  });

  it("says a stale decision revision in the form", async () => {
    api.answer = (request) =>
      request.method === "POST" ? Response.json({ error: "conflict", message: "their revision is 5" }, { status: 409 }) : Response.json(LIST);
    withList(<NodeDecisions project="demo" nodeId={NODE} />);
    await userEvent.type(screen.getByTestId("kg-decision-reason"), "why");
    await userEvent.click(screen.getByTestId("kg-decision-take"));
    expect(await screen.findByText("Ai đó vừa quyết định trước bạn. Hãy tải lại các quyết định rồi thử lại.")).toBeInTheDocument();
  });

  it("gives a reader the decisions without forms or steps", () => {
    withList(<NodeDecisions project="demo" nodeId={NODE} />, { ...LIST, can_decide: false });
    expect(screen.getAllByTestId("kg-decision")).toHaveLength(2);
    expect(screen.queryByTestId("kg-decision-take-form")).toBeNull();
    expect(screen.queryByTestId("kg-decision-accept")).toBeNull();
    expect(screen.queryByTestId("kg-decision-undo")).toBeNull();
  });

  it("lists the decisions about a cluster's members", () => {
    withList(<ClusterDecisions project="demo" ids={["app:file:other.py", "app:file:gone.py"]} />);
    expect(screen.getAllByTestId("kg-decision").map((li) => li.dataset.number)).toEqual(["3", "2"]);
    expect(screen.queryByTestId("kg-decision-take-form")).toBeNull();
  });

  it("lets a writer accept a proposed decision and undo an orphaned one on the review page", async () => {
    withList(<ReviewDecisions project="demo" />);
    const proposals = screen.getByTestId("kg-decision-proposals");
    expect(within(proposals).getAllByTestId("kg-decision").map((li) => li.dataset.number)).toEqual(["4"]);
    expect(within(proposals).getByTestId("kg-decision-reject")).toBeInTheDocument();
    const orphans = screen.getByTestId("kg-decision-orphans");
    expect(within(orphans).getByTestId("kg-decision-state")).toHaveTextContent("Mồ côi");
    expect(within(orphans).getByTestId("kg-decision-undo")).toBeInTheDocument();
    await userEvent.click(within(proposals).getByTestId("kg-decision-accept"));
    await userEvent.type(within(proposals).getByTestId("kg-decision-reason"), "right");
    await userEvent.click(within(proposals).getByTestId("kg-decision-step-save"));
    const post = await posted();
    expect(new URL(post.url).pathname).toBe("/v1/kg/demo/decisions/4/accept");
    expect(await post.json()).toEqual({ reason: "right", base_rev: 4 });
    await waitFor(() => expect(api.seen.some((r) => r.method === "GET" && new URL(r.url).pathname === "/v1/kg/demo/decisions")).toBe(true));
  });

  it("says when no decision waits for review", () => {
    withList(<ReviewDecisions project="demo" />, { ...LIST, decisions: [decision(1)] });
    expect(screen.getByTestId("kg-decision-proposals-empty")).toHaveTextContent("Không có đề xuất nào");
    expect(screen.getByTestId("kg-decision-orphans-empty")).toHaveTextContent("Không có quyết định nào mồ côi");
  });
});
