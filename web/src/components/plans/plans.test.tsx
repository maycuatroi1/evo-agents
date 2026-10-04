// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { planKeys } from "@/lib/plan-queries";
import type { Plan, PlanDiff, PlanSummary } from "@/lib/plans";
import { renderVi } from "@/test/render";

import { DiffView } from "./plan-diff";
import { PlanOverview } from "./plan-overview";
import { PlansList } from "./plans-list";
import { orderedEntries } from "./prose";
import { StepStatusBadge } from "./status";
import { StepDetail } from "./step-detail";

vi.mock("next/navigation", () => ({
  usePathname: () => "/p/demo/plans",
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), prefetch: vi.fn() }),
}));

const EVIDENCE = "evo-agents@0960c9b: 41 test xanh.\n  Dòng thụt lề   giữ nguyên <script>.\n";

const plan: Plan = {
  project: "demo",
  plan_id: "agent-hub",
  area: "active",
  revision: 4,
  digest: "sha256:0",
  label: { level: "internal" },
  created_at: "2026-10-01T02:00:00Z",
  updated_at: "2026-10-04T02:00:00Z",
  updated_by: "alice",
  body: {
    id: "agent-hub",
    title: "Hub cho agent",
    goal: "Một hub chung.",
    steps: [
      { id: 24, title: "Web shell", repo: "evo-agents", what: "Next.js", status: "done", evidence: EVIDENCE, done_at: "2026-10-03" },
      { id: 26, title: "Web: plans", repo: "evo-agents", what: "Trang plans", depends_on: [24], status: "pending", blocking: true },
    ],
  },
};

function withClient(ui: ReactElement, seed: (client: QueryClient) => void = () => undefined) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
  seed(client);
  return renderVi(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
}

beforeEach(() => {
  vi.stubGlobal(
    "matchMedia",
    vi.fn(() => ({ matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn() })),
  );
});

describe("StepStatusBadge", () => {
  it("says the status in words beside its icon", () => {
    renderVi(<StepStatusBadge group="blocked" />);
    const badge = screen.getByText("Bị chặn");
    expect(badge).toHaveAttribute("data-status", "blocked");
    expect(badge.querySelector("svg")).toHaveAttribute("aria-hidden", "true");
  });

  it("keeps an unknown status as written", () => {
    renderVi(<StepStatusBadge group="other" raw="skipped" />);
    expect(screen.getByText("Trạng thái khác: skipped")).toBeInTheDocument();
  });
});

describe("PlansList", () => {
  const summaries: PlanSummary[] = [
    { plan_id: "agent-hub", area: "active", revision: 4, digest: "d", title: "Hub cho agent", steps_total: 30, steps_done: 12, updated_at: "2026-10-04T02:00:00Z", updated_by: "alice" },
    { plan_id: "kg-prototype", area: "completed", revision: 1, digest: "d", title: null, steps_total: 13, steps_done: 13, updated_at: "2026-10-03T02:00:00Z", updated_by: "bob" },
  ];

  it("lists active and completed plans with steps done of total, and says the page is read-only", () => {
    withClient(<PlansList project="demo" initialError={null} />, (client) =>
      client.setQueryData(planKeys.all("demo"), summaries),
    );
    const active = within(screen.getByTestId("plans-active"));
    expect(active.getByRole("link", { name: "Hub cho agent" })).toHaveAttribute("href", "/p/demo/plans/agent-hub");
    expect(active.getByText("12/30 bước xong")).toBeInTheDocument();
    const completed = within(screen.getByTestId("plans-completed"));
    expect(completed.getByRole("link", { name: "kg-prototype" })).toBeInTheDocument();
    expect(completed.getByText("13/13 bước xong")).toBeInTheDocument();
    expect(screen.getByTestId("plans-read-only")).toHaveTextContent("evo harness step");
    expect(screen.getByTestId("plans-read-only")).toHaveTextContent("evo-agents hub plan");
    // The only buttons are the tables' sort buttons: nothing on the page changes a plan.
    const buttons = screen.getAllByRole("button").map((button) => button.textContent ?? "");
    expect(buttons.length).toBeGreaterThan(0);
    for (const name of buttons) expect(name).toMatch(/^(Plan|Tiến độ|Revision|Sửa lần cuối)/);
  });

  it("filters plans by title or id", async () => {
    withClient(<PlansList project="demo" initialError={null} />, (client) =>
      client.setQueryData(planKeys.all("demo"), summaries),
    );
    await userEvent.type(screen.getByRole("searchbox", { name: "Tìm plan theo tên hoặc mã" }), "kg-");
    expect(screen.queryByRole("link", { name: "Hub cho agent" })).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "kg-prototype" })).toBeInTheDocument();
    expect(screen.getByText("Hiện 1 trên 2 plan")).toBeInTheDocument();
  });

  it("shows the no-access state on a 403", () => {
    withClient(
      <PlansList project="demo" initialError={{ status: 403, code: "forbidden", message: "no grant", requestId: "r1" }} />,
    );
    expect(screen.getByTestId("state-forbidden")).toBeInTheDocument();
  });
});

describe("PlansList failures", () => {
  it("shows the request id and a retry on a 5xx", () => {
    withClient(
      <PlansList project="demo" initialError={{ status: 502, code: "bad_gateway", message: "down", requestId: "rid-7" }} />,
    );
    expect(screen.getByTestId("state-error")).toHaveTextContent("rid-7");
    expect(screen.getByRole("button", { name: "Thử lại" })).toBeInTheDocument();
  });

  it("says the project is not found on a 404", () => {
    withClient(<PlansList project="demo" initialError={{ status: 404, code: "not_found", message: "no", requestId: null }} />);
    expect(screen.getByTestId("state-not-found")).toHaveTextContent("Không tìm thấy dự án demo");
  });
});

describe("PlanOverview", () => {
  const full: Plan = {
    ...plan,
    body: {
      ...plan.body,
      risks: [{ mitigation: "đo trước", risk: "chậm" }],
      acceptance: ["chạy được"],
      zeta: "khoá lạ",
      decisions: [{ why: "vì", by: "binh", date: "2026-10-04", decision: "chỉ đọc" }],
    },
  };

  it("counts the steps by status, lays out the board and keeps the sections in the order plans are written", () => {
    withClient(<PlanOverview project="demo" planId="agent-hub" initialError={null} />, (client) =>
      client.setQueryData(planKeys.one("demo", "agent-hub"), full),
    );
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Hub cho agent");
    expect(screen.getByTestId("plan-progress")).toHaveTextContent("1 trên 2 bước đã xong (50%)");
    expect(within(screen.getByTestId("board-column-pending")).getByTestId("column-count")).toHaveTextContent("1");
    expect(within(screen.getByTestId("board-column-done")).getByTestId("column-count")).toHaveTextContent("1");
    const sections = screen.getAllByTestId(/^plan-section-/).map((node) => node.getAttribute("data-testid"));
    expect(sections).toEqual([
      "plan-section-acceptance",
      "plan-section-risks",
      "plan-section-decisions",
      "plan-section-zeta",
    ]);
    const decision = within(screen.getByTestId("plan-section-decisions")).getAllByRole("term");
    expect(decision.map((term) => term.textContent)).toEqual(["date", "by", "decision", "why"]);
    expect(screen.getByTestId("plans-read-only")).toBeInTheDocument();
  });

  it("says the plan is not found on a 404 and when the id cannot be a plan", () => {
    withClient(
      <PlanOverview project="demo" planId="gone" initialError={{ status: 404, code: "not_found", message: "", requestId: null }} />,
    );
    expect(screen.getByTestId("state-not-found")).toHaveTextContent("Không tìm thấy plan gone");
  });
});

describe("orderedEntries", () => {
  it("puts the known keys first, in order, then the rest alphabetically", () => {
    expect(orderedEntries({ z: 1, why: 2, a: 3, date: 4 }, ["date", "why"]).map(([key]) => key)).toEqual([
      "date",
      "why",
      "a",
      "z",
    ]);
  });
});

describe("StepDetail", () => {
  const show = (step: string) =>
    withClient(<StepDetail project="demo" planId="agent-hub" stepKey={step} initialError={null} />, (client) =>
      client.setQueryData(planKeys.one("demo", "agent-hub"), plan),
    );

  it("shows the evidence verbatim, every space and line break kept", () => {
    show("24");
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("24 Web shell");
    expect(screen.getByTestId("step-evidence").textContent).toBe(EVIDENCE);
    expect(screen.getByTestId("step-done-at")).toHaveAttribute("dateTime", "2026-10-03");
    expect(within(screen.getByTestId("step-facts")).getByText("Xong")).toBeInTheDocument();
  });

  it("links the steps it depends on and says when evidence is missing", () => {
    show("26");
    const facts = within(screen.getByTestId("step-facts"));
    expect(facts.getByRole("link", { name: "Bước 24: Web shell" })).toHaveAttribute("href", "/p/demo/plans/agent-hub/steps/24");
    expect(facts.getByText("Chặn tiến độ", { selector: "[data-blocking] *, [data-blocking]" })).toBeInTheDocument();
    expect(screen.getByText("Chưa có bằng chứng: bước chưa xong.")).toBeInTheDocument();
  });

  it("says when the plan has no such step", () => {
    show("99");
    expect(screen.getByTestId("state-not-found")).toHaveTextContent("Plan agent-hub không có bước 99");
  });
});

describe("DiffView", () => {
  const diff: PlanDiff = {
    plan_id: "agent-hub",
    from_revision: { revision: 1, area: "active", digest: "a", summary: "created", actor: "alice", created_at: "2026-10-01T02:00:00Z" },
    to_revision: { revision: 2, area: "active", digest: "b", summary: "step 26: status pending -> done", actor: "bob", created_at: "2026-10-02T02:00:00Z" },
    context: 3,
    added: 2,
    removed: 1,
    hunks: [
      {
        old_start: 10,
        old_lines: 2,
        new_start: 10,
        new_lines: 3,
        lines: [
          { kind: "context", old: 10, new: 10, text: "    what: Trang plans" },
          { kind: "removed", old: 11, new: null, text: "    status: pending" },
          { kind: "added", old: null, new: 11, text: "    status: done" },
          { kind: "added", old: null, new: 12, text: "    evidence: abc" },
        ],
      },
    ],
  };

  it("names each changed line in words and numbers it in its own revision", () => {
    renderVi(<DiffView diff={diff} />);
    const rows = screen.getAllByTestId("diff-line");
    expect(rows.map((row) => row.getAttribute("data-kind"))).toEqual(["context", "removed", "added", "added"]);
    expect(rows[1].textContent).toBe("11Xoá: -    status: pending");
    expect(rows[2].textContent).toBe("11Thêm: +    status: done");
    expect(screen.getByTestId("diff-stats")).toHaveTextContent("+2 dòng thêm");
    expect(screen.getByTestId("diff-stats")).toHaveTextContent("-1 dòng xoá");
    expect(screen.getByRole("table")).toHaveAccessibleName(/Đoạn 1, dòng 10 tới 11 của bản cũ và 10 tới 12 của bản mới/);
  });

  it("switches to side by side", async () => {
    renderVi(<DiffView diff={diff} />);
    await userEvent.click(screen.getByRole("button", { name: "Song song" }));
    expect(screen.getByRole("button", { name: "Song song" })).toHaveAttribute("aria-pressed", "true");
    const cells = screen.getAllByRole("cell").map((cell) => cell.textContent);
    expect(cells).toContain("Xoá: -    status: pending");
    expect(cells).toContain("Thêm: +    status: done");
  });

  it("says when two revisions read the same", () => {
    renderVi(<DiffView diff={{ ...diff, hunks: [], added: 0, removed: 0 }} />);
    expect(screen.getByTestId("diff-empty")).toHaveTextContent("Nội dung hai revision giống nhau.");
  });
});
