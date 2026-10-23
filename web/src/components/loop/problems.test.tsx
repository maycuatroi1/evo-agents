// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { describe, expect, it, vi } from "vitest";

import { ProblemChips } from "@/components/data/problem-chips";
import { loopKeys } from "@/lib/loop-queries";
import { problemKeys, type ProjectProblem, type ProjectProblemShown } from "@/lib/problem-queries";
import { queryKeys } from "@/lib/queries";
import { renderVi } from "@/test/render";

import { ProblemPage } from "./problem-page";
import { ProblemsPage } from "./problems-page";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  usePathname: () => "/p/northwind/problems",
  useSearchParams: () => new URLSearchParams(),
}));

const PROJECT = "northwind";

function problem(number: number, values: Partial<ProjectProblem> = {}): ProjectProblem {
  return {
    project: PROJECT,
    code: `P-${number}`,
    number,
    revision: 1,
    statement: `Shops lose a sale, case ${number}`,
    who: "Owners of small shops",
    when: "At lunch, when queues grow",
    workaround: "They take the order on paper",
    metric: null,
    rank: number,
    owner: "mai",
    status: "open",
    close_reason: null,
    note: null,
    created_by: "mai",
    created_at: "2026-10-20T08:00:00Z",
    ...values,
  };
}

const SHOWN: ProjectProblemShown = {
  ...problem(1, { revision: 3, status: "solved", close_reason: "Checkout takes 20 seconds now", created_by: "bao" }),
  goals: [{ code: "G-1", revision: 2, statement: "Shops check out in one tap", status: "active" }],
  claims: [
    { canvas: "northwind", canvas_name: "Northwind Checkout", code: "CL-1", cell: "problem", status: "testing", confidence: "medium", text: "Owners lose a sale at a slow checkout" },
    { canvas: "northwind", canvas_name: "Northwind Checkout", code: "CL-4", cell: "solution", status: "validated", confidence: "high", text: "One tap checkout" },
  ],
  plans: [{ plan_id: "northwind-one-tap", area: "active", title: "One tap checkout", status: "active", revision: 4, steps_total: 4, steps_done: 1 }],
};

function seeded(role: "reader" | "writer") {
  const client = new QueryClient({ defaultOptions: { queries: { staleTime: Infinity, retry: false } } });
  client.setQueryData(problemKeys.list(PROJECT), [problem(2, { rank: 1 }), problem(1, { status: "solved", rank: 2 }), problem(3, { rank: 2 })]);
  client.setQueryData(problemKeys.one(PROJECT, "P-1"), SHOWN);
  client.setQueryData(problemKeys.revisions(PROJECT, "P-1"), [
    { revision: 3, status: "solved", note: null, created_by: "bao", created_at: "2026-10-22T08:00:00Z", changed: ["status", "close_reason"] },
    { revision: 1, status: "open", note: "from the interviews", created_by: "mai", created_at: "2026-10-20T08:00:00Z", changed: ["statement", "who"] },
  ]);
  client.setQueryData(loopKeys.items(PROJECT, { state: null, kind: null, goal: null, open: false, problem: "P-1" }), { items: [], more: false });
  client.setQueryData(queryKeys.whoami, {
    login: "bao",
    admin: false,
    token: { kind: "web", scopes: [], expires_at: null },
    grants: [{ project: PROJECT, role, max_level: "internal" }],
  });
  return client;
}

function withClient(ui: ReactElement, client: QueryClient) {
  return renderVi(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
}

describe("the problems list", () => {
  it("lists the open problems by rank, then the closed ones, and filters them by status", async () => {
    withClient(<ProblemsPage project={PROJECT} initialError={null} />, seeded("writer"));
    const table = screen.getByTestId("problems-table");
    expect(within(table).getAllByTestId("problem-link").map((link) => link.getAttribute("data-problem"))).toEqual(["P-2", "P-3", "P-1"]);
    expect(within(table).getAllByTestId("problem-status").map((pill) => pill.textContent)).toEqual(["Đang mở", "Đang mở", "Đã giải"]);
    expect(screen.getByTestId("problems-new")).toHaveTextContent("Bài toán mới");
    await userEvent.click(within(screen.getByTestId("problem-statuses")).getByRole("button", { name: /Đã giải/ }));
    expect(within(screen.getByTestId("problems-table")).getAllByTestId("problem-link").map((link) => link.getAttribute("data-problem"))).toEqual(["P-1"]);
  });

  it("gives a reader no way to write", () => {
    withClient(<ProblemsPage project={PROJECT} initialError={null} />, seeded("reader"));
    expect(screen.getByTestId("problems-table")).toBeInTheDocument();
    expect(screen.queryByTestId("problems-new")).toBeNull();
  });
});

describe("a problem's page", () => {
  it("shows what hurts, why it closed, the goals, the claims by canvas and the plans that point to it, and its history", () => {
    withClient(<ProblemPage project={PROJECT} code="P-1" initialError={null} />, seeded("writer"));
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Shops lose a sale, case 1");
    expect(screen.getByTestId("problem-who")).toHaveTextContent("Owners of small shops");
    expect(screen.getByTestId("problem-closed")).toHaveTextContent("Checkout takes 20 seconds now");
    expect(screen.getByTestId("problem-reopen")).toBeInTheDocument();
    expect(screen.queryByTestId("problem-close")).toBeNull();
    expect(within(screen.getByTestId("problem-goals")).getByTestId("goal-status")).toHaveTextContent("Đang chạy");
    const canvas = within(screen.getByTestId("problem-claims")).getByTestId("problem-canvas");
    expect(within(canvas).getAllByTestId("claim-status").map((pill) => pill.textContent)).toEqual(["Đang kiểm", "Đã xác thực"]);
    expect(within(screen.getByTestId("problem-plans")).getByTestId("problem-plan-progress")).toHaveTextContent("1/4");
    expect(within(screen.getByTestId("problem-plans")).getByRole("progressbar")).toHaveAttribute("aria-valuenow", "25");
    expect(screen.getByTestId("problem-metric")).toHaveTextContent("không nêu chỉ số");
    expect(screen.getAllByTestId("problem-revision-row").map((row) => row.getAttribute("data-revision"))).toEqual(["3", "1"]);
    expect(screen.getByTestId("problem-loop")).toHaveTextContent("Loop chưa tạo work item nào");
  });

  it("tells a reader they only read, with no button that writes", () => {
    withClient(<ProblemPage project={PROJECT} code="P-1" initialError={null} />, seeded("reader"));
    expect(screen.getByTestId("problem-read-only")).toBeInTheDocument();
    expect(screen.queryByTestId("problem-edit")).toBeNull();
    expect(screen.queryByTestId("problem-reopen")).toBeNull();
  });
});

describe("the problem chips", () => {
  it("name each problem by its statement once the project's problems are read, and open its page", () => {
    withClient(<ProblemChips project={PROJECT} codes={["P-3", "P-9"]} />, seeded("reader"));
    const chips = screen.getAllByTestId("problem-chip");
    expect(chips.map((chip) => chip.getAttribute("href"))).toEqual(["/p/northwind/problems/P-3", "/p/northwind/problems/P-9"]);
    expect(chips[0]).toHaveAccessibleName("Bài toán P-3: Shops lose a sale, case 3");
    expect(chips[1]).toHaveAccessibleName("Bài toán P-9");
  });

  it("draw nothing for no problem", () => {
    const { container } = withClient(<ProblemChips project={PROJECT} codes={[]} />, seeded("reader"));
    expect(container.querySelector('[data-testid="problem-chips"]')).toBeNull();
  });
});
