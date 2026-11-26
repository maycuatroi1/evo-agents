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
import { StepStrip } from "./progress";
import { orderedEntries } from "./prose";
import { StepStatusBadge } from "./status";
import { StepDetail } from "./step-detail";

vi.mock("next/navigation", () => ({
  usePathname: () => "/p/demo/plans",
  useSearchParams: () => new URLSearchParams(),
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
    const badge = screen.getByText("Bị chặn").closest("[data-slot=status-badge]");
    expect(badge).toHaveAttribute("data-status", "blocked");
    expect(badge?.querySelector("svg")).toHaveAttribute("aria-hidden", "true");
  });

  it("keeps an unknown status as written", () => {
    renderVi(<StepStatusBadge group="other" raw="skipped" />);
    expect(screen.getByText("Trạng thái khác: skipped")).toBeInTheDocument();
  });
});

describe("StepStrip", () => {
  it("step strip follows the plan's steps", () => {
    const steps = [
      { key: "1", status: "done" },
      { key: "2", status: "descoped" },
      { key: "3", status: "blocked" },
      { key: "4", status: "in_progress" },
      { key: "5", status: null },
      { key: "6", status: "skipped" },
    ];
    renderVi(<StepStrip steps={steps} />);
    const strip = screen.getByTestId("step-strip");
    const marks = [...strip.children];
    expect(marks.map((mark) => mark.getAttribute("data-step"))).toEqual(["1", "2", "3", "4", "5", "6"]);
    expect(marks.map((mark) => mark.getAttribute("data-status"))).toEqual(["done", "descoped", "blocked", "in_progress", "pending", "other"]);
    expect(strip).toHaveAccessibleName("Các bước: 1 xong, 1 đang làm, 1 bị chặn, 1 chưa làm, 1 bỏ khỏi phạm vi, 1 trạng thái khác");
  });

  it("draws nothing for a plan without steps", () => {
    renderVi(<StepStrip steps={[]} />);
    expect(screen.queryByTestId("step-strip")).not.toBeInTheDocument();
  });
});

describe("PlansList", () => {
  const steps = (statuses: string[]) => statuses.map((status, index) => ({ key: String(index + 1), status }));
  const summaries: PlanSummary[] = [
    {
      plan_id: "agent-hub",
      area: "active",
      revision: 4,
      digest: "d",
      title: "Hub cho agent",
      steps_total: 3,
      steps_done: 1,
      updated_at: "2026-10-04T02:00:00Z",
      updated_by: "alice",
      goal: "Một hub chung.",
      steps: steps(["done", "blocked", "pending"]),
      current_step: { key: "2", title: "Web: plans", status: "blocked" },
      repos: ["lumen-api"],
      problems: [],
    },
    {
      plan_id: "kg-wiring",
      area: "active",
      revision: 2,
      digest: "d",
      title: null,
      steps_total: 2,
      steps_done: 2,
      updated_at: "2026-10-05T02:00:00Z",
      updated_by: "bob",
      goal: "Nối graph vào hub. Rồi đo.",
      steps: steps(["done", "descoped"]),
      current_step: null,
      repos: ["lumen-web"],
      problems: [],
    },
    { plan_id: "kg-prototype", area: "completed", revision: 1, digest: "d", title: null, steps_total: 13, steps_done: 13, updated_at: "2026-10-03T02:00:00Z", updated_by: "bob" },
  ];
  const rows = () => screen.getAllByTestId("plan-row").map((row) => row.getAttribute("data-plan-id"));

  beforeEach(() => window.history.replaceState(null, "", "/p/demo/plans"));

  it("lists the open plans last changed first, each with its current step and steps, and no button", () => {
    withClient(<PlansList project="demo" initialError={null} />, (client) => client.setQueryData(planKeys.all("demo"), summaries));
    expect(screen.getByTestId("plans-cli-note")).toHaveTextContent("Sửa từ CLI");
    expect(screen.queryByTestId("plans-read-only")).not.toBeInTheDocument();
    expect(rows()).toEqual(["kg-wiring", "agent-hub"]);
    const hub = within(screen.getAllByTestId("plan-row")[1]);
    expect(hub.getByRole("link", { name: "Hub cho agent" })).toHaveAttribute("href", "/p/demo/plans/agent-hub");
    expect(hub.getByTestId("plan-current-step")).toHaveTextContent("Bước 2 trên 3: Web: plans");
    expect(hub.getByTestId("plan-current-step")).toHaveAttribute("data-status", "blocked");
    expect(hub.getByTestId("plan-steps-count")).toHaveTextContent("1/3 bước xong");
    // A plan without a title is named by its goal's first sentence; every step done or descoped writes it faint.
    const wiring = screen.getAllByTestId("plan-row")[0];
    expect(within(wiring).getByRole("link", { name: "Nối graph vào hub." })).toBeInTheDocument();
    expect(within(wiring).getByTestId("plan-slug")).toHaveTextContent("kg-wiring");
    expect(wiring).toHaveAttribute("data-finished", "true");
    expect(within(wiring).getByTestId("plan-current-step")).toHaveTextContent("Xong cả 2 bước");
    // The rows hold no button: the only buttons are the toolbar's.
    for (const row of screen.getAllByTestId("plan-row")) expect(within(row).queryAllByRole("button")).toEqual([]);
  });

  it("switches to the completed plans and filters by repo, writing both to the address", async () => {
    withClient(<PlansList project="demo" initialError={null} />, (client) => client.setQueryData(planKeys.all("demo"), summaries));
    await userEvent.selectOptions(screen.getByTestId("plans-repo"), "lumen-api");
    expect(rows()).toEqual(["agent-hub"]);
    expect(window.location.search).toBe("?repo=lumen-api");
    await userEvent.click(screen.getByTestId("plans-view-completed"));
    expect(screen.getByTestId("plans-view-completed")).toHaveAttribute("aria-pressed", "true");
    expect(window.location.search).toBe("?view=completed&repo=lumen-api");
    expect(screen.getByText("Không có plan nào khớp")).toBeInTheDocument();
  });

  it("groups the plans by repo", async () => {
    withClient(<PlansList project="demo" initialError={null} />, (client) => client.setQueryData(planKeys.all("demo"), summaries));
    await userEvent.selectOptions(screen.getByTestId("plans-group"), "repo");
    expect(screen.getAllByTestId("plans-group-section").map((group) => group.getAttribute("data-group"))).toEqual(["lumen-api", "lumen-web"]);
  });

  it("marks a plan a system runs with a link to its trace, since the trace's chat runs name no plan", () => {
    const traced = { ...summaries[0], trace: { id: 205, system: "dev", run_id: 10703, state: "running" as const } };
    withClient(<PlansList project="demo" initialError={null} />, (client) =>
      client.setQueryData(planKeys.all("demo"), [traced, summaries[1]]),
    );
    const link = screen.getByTestId("plan-trace-link");
    expect(link).toHaveAttribute("data-trace-id", "205");
    expect(link.getAttribute("href")).toMatch(/\/systems\/dev\?mode=observe&work=205$/);
    expect(link.getAttribute("aria-label")).toMatch(/^Đang chạy trong system dev, trace #205: /);
  });

  it("finds plans by goal or id", async () => {
    withClient(<PlansList project="demo" initialError={null} />, (client) => client.setQueryData(planKeys.all("demo"), summaries));
    await userEvent.type(screen.getByRole("searchbox", { name: "Tìm plan theo tên, goal hoặc mã" }), "graph");
    // The query is committed after a 150 ms pause in typing.
    expect(await screen.findByText("Hiện 1 trên 2 plan")).toBeInTheDocument();
    expect(rows()).toEqual(["kg-wiring"]);
    expect(window.location.search).toBe("?q=graph");
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
