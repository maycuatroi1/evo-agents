// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { createApiClient } from "@/lib/api/client";
import { queryKeys } from "@/lib/queries";
import { renderVi } from "@/test/render";

import { HomePage } from "./home-page";
import type { Overview, OverviewDecision, OverviewRun } from "./model";

const api = vi.hoisted(() => ({ answers: {} as Record<string, unknown>, seen: [] as string[] }));

vi.mock("@/lib/api/browser", () => ({
  browserApi: () =>
    createApiClient({
      baseUrl: "http://hub.test",
      fetch: async (request) => {
        const path = new URL(request.url).pathname;
        api.seen.push(`${request.method} ${path}`);
        if (path === "/v1/auth/web/csrf") return Response.json({ csrf: "csrf-1", header: "X-Evo-CSRF" });
        if (!(path in api.answers)) return Response.json({ error: "not_found", message: "no such thing" }, { status: 404 });
        return Response.json(api.answers[path]);
      },
    }),
}));

vi.mock("next/navigation", () => ({ usePathname: () => "/", useRouter: () => ({ push: vi.fn() }) }));

beforeAll(() => {
  // Recharts measures its container; jsdom has no layout.
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  );
});

function run(id: number, state: OverviewRun["state"], extra: Partial<OverviewRun> = {}): OverviewRun {
  return {
    id,
    kind: "step",
    project: "demo",
    plan_id: "rollout",
    plan_title: "Worker fleet rollout",
    step_key: "2",
    title: "Queue with dispatch and claim",
    state,
    dispatched_by: "octo",
    worker_id: 1,
    worker: "mini",
    runtime: "claude-code",
    model: null,
    steps_total: null,
    steps_done: null,
    run_seconds: 0,
    queued_at: "2026-10-07T01:00:00Z",
    started_at: "2026-10-07T01:01:00Z",
    finished_at: null,
    error: null,
    ...extra,
  };
}

function decision(id: number, yours: boolean): OverviewDecision {
  return {
    id,
    project: "demo",
    run_id: 12,
    run_state: "waiting",
    plan_id: "fleet",
    plan_title: "Plan runs on the web",
    step_key: "3",
    category: "deploy",
    question: yours ? "Deploy to staging now?" : "Drop the old bucket?",
    owner: yours ? "octo" : "hubot",
    yours,
    asked_at: "2026-10-07T01:00:00Z",
    parks_at: "2026-10-08T01:00:00Z",
  };
}

const DAYS = ["2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04", "2026-10-05", "2026-10-06", "2026-10-07"];

function overview(extra: Partial<Overview> = {}): Overview {
  return {
    counts: { waiting_on_you: 1, running: 1, queued: 1, done_7d: 3, failed_7d: 1, lost_7d: 1 },
    done_by_day: DAYS.map((day, index) => ({ day, done: index === 6 ? 2 : index === 3 ? 1 : 0 })),
    active_runs: [
      run(13, "running", { kind: "plan", step_key: null, title: "Plan runs on the web", plan_id: "fleet", plan_title: "Plan runs on the web", steps_done: 1, steps_total: 4 }),
      run(12, "waiting", { kind: "plan", step_key: null, plan_id: "fleet", plan_title: "Plan runs on the web", steps_done: 2, steps_total: 4 }),
      run(14, "queued", { worker_id: null, worker: null, runtime: "any", started_at: null }),
    ],
    recent_runs: [
      run(9, "done", { finished_at: "2026-10-07T01:09:12Z" }),
      run(8, "failed", { error: "The agent did not write .evo-run/result.json", finished_at: "2026-10-07T00:50:00Z" }),
      run(7, "failed", { dispatched_by: "hubot", finished_at: "2026-10-06T23:00:00Z" }),
    ],
    open_decisions: [decision(7, true), decision(8, false)],
    author_waiting: [],
    projects: [
      { name: "demo", role: "writer", max_level: "internal", repos: 2, active_plans: 3, open_decisions: 2 },
      { name: "docs", role: "reader", max_level: "public", repos: 1, active_plans: 0, open_decisions: 0 },
    ],
    ...extra,
  };
}

function setup(data: Overview, { admin = false, workers = [] as unknown[] } = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity }, mutations: { retry: false } } });
  const whoami = { login: "octo", admin, token: {}, grants: data.projects.map((project) => ({ project: project.name, role: project.role, max_level: project.max_level })) };
  client.setQueryData(queryKeys.whoami, whoami);
  client.setQueryData(queryKeys.overview, data);
  api.answers["/v1/me/overview"] = data;
  api.answers["/v1/auth/whoami"] = whoami;
  api.answers["/v1/workers"] = workers;
  renderVi(
    <QueryClientProvider client={client}>
      <HomePage initialError={null} />
    </QueryClientProvider>,
  );
  return userEvent.setup();
}

beforeEach(() => {
  api.answers = {};
  api.seen = [];
});

describe("HomePage", () => {
  it("counts what waits, runs and ended in one strip, the done days named in words", async () => {
    setup(overview());
    expect(screen.getByRole("heading", { level: 1, name: "Trang chủ" })).toBeInTheDocument();
    const strip = screen.getByTestId("home-metrics");
    expect(within(strip).getByTestId("summary-waiting-value")).toHaveTextContent("1");
    expect(within(strip).getByTestId("summary-waiting-value").className).toContain("text-attention");
    expect(within(strip).getByTestId("summary-running")).toHaveTextContent("#13 trên mini");
    expect(within(strip).getByTestId("summary-queued")).toHaveTextContent("#14, vào hàng đợi");
    expect(within(strip).getByTestId("summary-done-value")).toHaveTextContent("3");
    expect(within(strip).getByRole("img", { name: /^Run xong mỗi ngày, từ .+: 0, 0, 0, 1, 0, 0, 2$/ })).toBeInTheDocument();
    expect(within(strip).getByTestId("summary-failed-value")).toHaveTextContent("1và 1 mất lease");
    expect(within(strip).getByTestId("summary-failed")).toHaveTextContent("#8: The agent did not write .evo-run/result.json");
  });

  it("lists in Needs you only the decisions the visitor answers, and the others on their runs in In flight", () => {
    setup(overview());
    const needs = screen.getByRole("region", { name: "Cần bạn" });
    const items = within(needs).getAllByTestId("needs-you-item");
    expect(items).toHaveLength(1);
    expect(within(items[0]).getByRole("link", { name: "Deploy to staging now?" })).toHaveAttribute("href", "/inbox?decision=7");
    expect(within(items[0]).getByRole("button", { name: "Trả lời quyết định #7" })).toBeInTheDocument();
    expect(within(needs).getByText("1 quyết định chờ bạn trả lời")).toHaveClass("sr-only");

    const flight = screen.getByRole("region", { name: "Đang diễn ra" });
    const rows = within(flight).getAllByTestId("in-flight-item");
    expect(rows.map((row) => row.getAttribute("data-run-id"))).toEqual(["13", "12", "14"]);
    // The plan run at work: a live dot, its steps done of the total, its worker.
    expect(rows[0].querySelector(".animate-live-ping")).not.toBeNull();
    expect(within(rows[0]).getByTestId("home-plan-progress")).toHaveTextContent("xong 1 trên 4 bước");
    expect(within(rows[0]).getByText("mini")).toBeInTheDocument();
    // The waiting plan run names whose answer it waits for; the run of the open decision is the visitor's.
    expect(within(rows[1]).getByTestId("run-state")).toHaveTextContent("Đang chờ bạn");
    expect(within(rows[2]).getByText("chưa có worker")).toBeInTheDocument();
  });

  it("lists the visitor's author runs waiting for a reply, with the agent's last message, linked to their chat", () => {
    setup(
      overview({
        author_waiting: [
          {
            id: 21,
            project: "demo",
            plan_id: null,
            plan_title: null,
            title: "Let members write plans from the web",
            state: "waiting",
            worker: "mini",
            waiting_since: "2026-10-07T01:00:00Z",
            parked_at: null,
            message: "Should New plan sit on the Plans page?",
          },
          {
            id: 19,
            project: "demo",
            plan_id: "rollout",
            plan_title: "Worker fleet rollout",
            title: "Split step 4",
            state: "parked",
            worker: "mini",
            waiting_since: null,
            parked_at: "2026-10-06T01:00:00Z",
            message: null,
          },
        ],
      }),
    );
    const card = screen.getByRole("region", { name: "Đang chờ bạn trả lời" });
    expect(within(card).getByText("2 author run đang chờ bạn trả lời")).toHaveClass("sr-only");
    const items = within(card).getAllByTestId("author-waiting-item");
    expect(items.map((item) => item.getAttribute("data-run-id"))).toEqual(["21", "19"]);
    expect(within(items[0]).getByRole("link", { name: "Should New plan sit on the Plans page?" })).toHaveAttribute("href", "/p/demo/runs/21?tab=chat");
    expect(items[0]).toHaveTextContent("demo, plan mới");
    expect(within(items[0]).getByRole("link", { name: "Trả lời trong chat của run #21" })).toHaveAttribute("href", "/p/demo/runs/21?tab=chat");
    // Without a message yet, the run's title stands in; a parked one says since when.
    expect(within(items[1]).getByRole("link", { name: "Split step 4" })).toBeInTheDocument();
    expect(items[1]).toHaveTextContent("demo, Worker fleet rollout");
    expect(items[1]).toHaveTextContent("đã park");
  });

  it("hides Needs you when nothing waits for the visitor, and gives the strip to the quiet line when nothing is in flight", () => {
    setup(
      overview({
        counts: { waiting_on_you: 0, running: 0, queued: 0, done_7d: 3, failed_7d: 1, lost_7d: 0 },
        active_runs: [],
        open_decisions: [],
      }),
    );
    expect(screen.queryByTestId("needs-you")).toBeNull();
    expect(screen.queryByTestId("in-flight")).toBeNull();
    expect(screen.getByTestId("home-quiet")).toHaveTextContent(
      "Mọi thứ yên ắng. Không run nào đang chạy và không có gì chờ bạn. 7 ngày qua: 3 xong, 1 thất bại, 0 mất lease.",
    );
    expect(screen.getByTestId("recent")).toBeInTheDocument();
  });

  it("offers Rerun on the visitor's own failed run only, and reports the new run in a toast", async () => {
    api.answers["/v1/projects/demo/runs/8/rerun"] = { ...run(15, "queued"), id: 15 };
    const user = setup(overview());
    const recent = screen.getByRole("region", { name: "Vừa kết thúc" });
    const rows = within(recent).getAllByTestId("recent-item");
    expect(within(rows[0]).queryByTestId("recent-rerun")).toBeNull();
    expect(within(rows[2]).queryByTestId("recent-rerun")).toBeNull(); // hubot's
    expect(within(rows[1]).getByTestId("recent-error")).toHaveTextContent("The agent did not write .evo-run/result.json");
    await user.click(within(rows[1]).getByRole("button", { name: "Chạy lại #8" }));
    await waitFor(() => expect(api.seen).toContain("POST /v1/projects/demo/runs/8/rerun"));
    expect(await screen.findByText("Đã dispatch run #15")).toBeInTheDocument();
  });

  it("lists the projects with role, active plans, repos and open decisions", () => {
    setup(overview());
    const projects = within(screen.getByTestId("home-projects")).getAllByTestId("home-project");
    expect(projects.map((row) => row.getAttribute("data-project"))).toEqual(["demo", "docs"]);
    expect(within(projects[0]).getByTestId("home-project-facts")).toHaveTextContent("Ghi, 3 plan đang chạy, 2 kho mã");
    expect(within(projects[0]).getByTestId("home-project-decisions")).toHaveTextContent("22 quyết định đang mở");
    expect(within(projects[1]).queryByTestId("home-project-decisions")).toBeNull();
    expect(within(projects[0]).getByRole("link", { name: "Mở dự án demo" })).toHaveAttribute("href", "/p/demo");
  });

  it("shows the visitor's own workers in the Fleet card", async () => {
    setup(overview(), {
      workers: [
        { id: 1, name: "mini", owner: "octo", status: "online", held_runs: 1, slots: 1, os: "macOS 15", arch: "arm64", runtimes: { "claude-code": { available: true } }, created_at: "2026-10-01T00:00:00Z", last_heartbeat_at: "2026-10-07T01:00:00Z" },
        { id: 2, name: "theirs", owner: "hubot", status: "online", held_runs: 0, slots: 1, os: "Linux", arch: "x86_64", runtimes: {}, created_at: "2026-10-01T00:00:00Z", last_heartbeat_at: null },
      ],
    });
    const fleet = screen.getByTestId("fleet");
    const workers = await within(fleet).findAllByTestId("fleet-worker");
    expect(workers).toHaveLength(1);
    expect(workers[0]).toHaveAttribute("data-status", "busy");
    expect(within(workers[0]).getByTestId("worker-status")).toHaveTextContent("Bận 1/1");
    expect(workers[0]).toHaveTextContent("Claude Code, macOS 15 arm64, heartbeat gần nhất");
  });

  it("tells a member without a grant who grants roles, with their login to send", () => {
    setup(overview({ projects: [], active_runs: [], recent_runs: [], open_decisions: [] }));
    const empty = screen.getByTestId("state-empty");
    expect(empty).toHaveTextContent("Bạn chưa được cấp dự án nào");
    expect(empty).toHaveTextContent("Quản trị viên hub cấp vai trò trong từng dự án. Gửi họ tên đăng nhập của bạn, octo");
    expect(within(empty).getByRole("button", { name: "Sao chép tên đăng nhập" })).toBeInTheDocument();
    expect(screen.queryByTestId("home-metrics")).toBeNull();
  });

  it("sends a hub admin without a grant to Administration", () => {
    setup(overview({ projects: [], active_runs: [], recent_runs: [], open_decisions: [] }), { admin: true });
    expect(within(screen.getByTestId("state-empty")).getByRole("link", { name: "Mở trang Quản trị" })).toHaveAttribute("href", "/admin/members");
  });
});
