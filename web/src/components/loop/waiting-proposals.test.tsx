// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { createApiClient } from "@/lib/api/client";
import { problemKeys } from "@/lib/problem-queries";
import { queryKeys } from "@/lib/queries";
import { type BuilderProposal, proposalMakes } from "@/lib/waiting-proposals";
import { renderVi } from "@/test/render";

import { ProblemsPage } from "./problems-page";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  usePathname: () => "/p/northwind/problems",
  useSearchParams: () => new URLSearchParams(),
}));

const api = vi.hoisted(() => ({
  waiting: [] as unknown[],
  answered: null as null | ((answer: string) => unknown),
  seen: [] as Request[],
}));

vi.mock("@/lib/api/browser", () => ({
  browserApi: () =>
    createApiClient({
      baseUrl: "http://hub.test",
      fetch: async (request) => {
        api.seen.push(request);
        const url = new URL(request.url);
        if (url.pathname === "/v1/auth/web/csrf") return Response.json({ csrf: "csrf-1", header: "X-Evo-CSRF" });
        if (url.pathname === "/v1/x/builder/northwind/proposals/waiting") return Response.json(api.waiting);
        if (url.pathname === "/v1/x/builder/northwind/proposals/13/answer" && api.answered) {
          const { answer } = (await request.clone().json()) as { answer: string };
          return Response.json(api.answered(answer));
        }
        if (url.pathname === "/v1/projects/northwind/problems") return Response.json([]);
        throw new Error(`no answer for ${request.method} ${url.pathname}`);
      },
    }),
}));

const PROJECT = "northwind";
const LINE = {
  source_kind: "messenger",
  source_uri: "messenger://shop-group/1",
  excerpt: "We ring the depot every morning.",
  signal: "workaround",
  label: "internal",
};

function proposal(values: Partial<BuilderProposal> = {}): BuilderProposal {
  return {
    id: 13,
    project: PROJECT,
    kind: "problem",
    target: { problem: "P-1" },
    change: {
      set: { statement: "Shops cannot tell when a delivery comes", who: "shop owners", when: "mornings", workaround: "they ring the depot" },
      unset: [],
      evidence: [LINE, { ...LINE, source_kind: "email", source_uri: "email://depot/42" }],
    },
    summary: "Intake I-1: a new problem, from 2 lines: Shops cannot tell when a delivery comes",
    base_version: 0,
    state: "pending",
    approver: "project_writer",
    proposed_by: "alice",
    proposed_at: "2026-10-25T08:00:00Z",
    decided_by: null,
    decided_at: null,
    applied_version: null,
    rollback_version: null,
    error: null,
    run_id: null,
    link: null,
    ...values,
  };
}

function page(role: "reader" | "writer"): ReactElement {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  client.setQueryData(problemKeys.list(PROJECT), []);
  client.setQueryData(queryKeys.whoami, {
    login: "bao",
    admin: false,
    token: { kind: "web", scopes: [], expires_at: null },
    grants: [{ project: PROJECT, role, max_level: "internal" }],
  });
  return (
    <QueryClientProvider client={client}>
      <ProblemsPage project={PROJECT} initialError={null} />
    </QueryClientProvider>
  );
}

beforeEach(() => {
  api.waiting = [proposal()];
  api.answered = null;
  api.seen = [];
});

describe("the proposals that wait for a writer", () => {
  it("list an intake's proposal above the problems, with its lines of evidence, and approving it makes the problem", async () => {
    api.answered = (answer) => proposal({ state: answer === "approve" ? "applied" : "rejected", decided_by: "bao", applied_version: 1 });
    renderVi(page("writer"));
    const item = await screen.findByTestId("waiting-proposal");
    expect(screen.getByTestId("waiting-proposals")).toHaveTextContent("1 đề xuất đang chờ bạn trả lời");
    expect(screen.getByTestId("problems-empty")).toBeInTheDocument();
    expect(within(item).getByText("Shops cannot tell when a delivery comes")).toBeInTheDocument();
    expect(within(item).getByTestId("waiting-proposal-meta")).toHaveTextContent("P-1 mới, từ 2 dòng bằng chứng. Gây khó cho: shop owners. Đề xuất bởi alice");
    expect(within(item).getByTestId("waiting-proposal-lines")).toHaveTextContent("We ring the depot every morning.");
    expect(within(item).queryByTestId("waiting-proposal-thread")).toBeNull();

    api.waiting = [];
    await userEvent.click(within(item).getByTestId("waiting-proposal-approve"));
    expect(await screen.findByText("Đã tạo P-1")).toBeInTheDocument();
    const post = api.seen.find((request) => request.method === "POST");
    expect(post?.headers.get("X-Evo-CSRF")).toBe("csrf-1");
    expect(await post?.clone().json()).toEqual({ answer: "approve" });
    await waitFor(() => expect(screen.queryByTestId("waiting-proposals")).toBeNull());
  });

  it("reject one after a confirmation", async () => {
    api.answered = (answer) => proposal({ state: answer === "reject" ? "rejected" : "applied", decided_by: "bao" });
    renderVi(page("writer"));
    await userEvent.click(within(await screen.findByTestId("waiting-proposal")).getByTestId("waiting-proposal-reject"));
    const dialog = screen.getByTestId("waiting-proposal-reject-dialog");
    expect(dialog).toHaveTextContent("Từ chối đề xuất 13?");
    expect(api.seen.some((request) => request.method === "POST")).toBe(false);
    await userEvent.click(within(dialog).getByRole("button", { name: "Từ chối" }));
    expect(await screen.findByText("Đã từ chối đề xuất 13")).toBeInTheDocument();
    expect(await api.seen.find((request) => request.method === "POST")?.clone().json()).toEqual({ answer: "reject" });
  });

  it("are not asked for, nor shown, for a reader", async () => {
    renderVi(page("reader"));
    expect(await screen.findByTestId("problems-empty")).toBeInTheDocument();
    expect(screen.queryByTestId("waiting-proposals")).toBeNull();
    expect(api.seen.some((request) => request.url.includes("/proposals/waiting"))).toBe(false);
  });
});

describe("what a proposal makes", () => {
  it("is a new one at version 0, lines of evidence alone, or a change", () => {
    expect(proposalMakes(proposal(), "problem")).toMatchObject({ what: "new", code: "P-1", statement: "Shops cannot tell when a delivery comes" });
    const lines = proposal({ base_version: 2, change: { set: {}, unset: [], evidence: [LINE] } });
    expect(proposalMakes(lines, "problem")).toMatchObject({ what: "evidence", code: "P-1", statement: null });
    expect(proposalMakes(lines, "problem").lines).toHaveLength(1);
    const edit = proposal({ base_version: 2, change: { set: { rank: 3 }, unset: [] } });
    expect(proposalMakes(edit, "problem")).toMatchObject({ what: "change", lines: [] });
  });
});
