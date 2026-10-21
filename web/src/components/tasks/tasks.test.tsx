// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { afterAll, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { queryKeys } from "@/lib/queries";
import { detailOf, SETTINGS, SPRINTS, TASKS } from "@/test/fixtures/tasks";
import { renderVi } from "@/test/render";

import type { Task, TaskDetail, TaskEdit, TaskLink } from "./api";
import { tasksKeys } from "./api";
import { BoardPage } from "./board-page";
import { TaskLinks } from "./task-links";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  usePathname: () => "/p/demo/x/tasks/board",
  useSearchParams: () => new URLSearchParams(),
}));

const PROJECT = "demo";
const SPRINT_7 = { kind: "sprint", name: "Sprint 7" } as const;
const PEOPLE = [
  { login: "alice", role: "admin" },
  { login: "bao", role: "writer" },
  { login: "chi", role: "writer" },
];

/**
 * What the API answers in these tests: the CSRF token, the board's reads from `rows` (what the hub holds now), and a
 * change of a task as `answer` makes it. `sent` records every request but the reads.
 */
let answer: (ref: string, change: TaskEdit) => Response = () => new Response(null, { status: 500 });
let rows: Task[] = TASKS;
const sent: { method: string; path: string; body: unknown }[] = [];
const READS: Record<string, () => unknown> = {
  "/v1/x/tasks/demo/tasks": () => rows,
  "/v1/x/tasks/demo/sprints": () => SPRINTS,
  "/v1/x/tasks/demo/settings": () => SETTINGS,
  "/v1/x/tasks/demo/people": () => PEOPLE,
};

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

beforeAll(() => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (request: Request) => {
      const { pathname } = new URL(request.url);
      if (pathname === "/v1/auth/web/csrf") return json({ csrf: "token", header: "X-Evo-CSRF" });
      if (request.method === "GET" && READS[pathname]) return json(READS[pathname]());
      const body: unknown = request.method === "GET" ? null : await request.json();
      sent.push({ method: request.method, path: pathname, body });
      const edited = /^\/v1\/x\/tasks\/demo\/tasks\/([^/]+)$/.exec(pathname);
      if (request.method === "PATCH" && edited) return answer(decodeURIComponent(edited[1]), body as TaskEdit);
      return json({ code: "not_found", message: "no API in unit tests" }, 404);
    }),
  );
});
afterAll(() => vi.unstubAllGlobals());
beforeEach(() => {
  sent.length = 0;
  rows = TASKS;
});

function withClient(ui: ReactElement, client: QueryClient) {
  return renderVi(<QueryClientProvider client={client}>{ui}</QueryClientProvider>);
}

/** A client that holds the board of Sprint 7, the sprints, the people and the visitor, and never asks for them. */
function seeded(role: "reader" | "writer") {
  const client = new QueryClient({ defaultOptions: { queries: { staleTime: Infinity, retry: false } } });
  client.setQueryData(tasksKeys.settings(PROJECT), SETTINGS);
  client.setQueryData(tasksKeys.sprints(PROJECT), SPRINTS);
  client.setQueryData(tasksKeys.people(PROJECT), PEOPLE);
  client.setQueryData(tasksKeys.list(PROJECT, SPRINT_7), TASKS);
  client.setQueryData(queryKeys.whoami, {
    login: "bao",
    admin: false,
    token: { kind: "web", scopes: [], expires_at: null },
    grants: [{ project: PROJECT, role, max_level: "internal" }],
  });
  return client;
}

function column(status: string) {
  return screen.getAllByTestId("board-column").find((one) => one.dataset.status === status)!;
}

function refsIn(status: string) {
  return within(column(status))
    .queryAllByTestId("task-card")
    .map((card) => card.dataset.task);
}

describe("BoardPage", () => {
  it("opens on the active sprint with a column per status, the cancelled tasks off the board", () => {
    withClient(<BoardPage project={PROJECT} title="Tasks" ask={null} initialError={null} />, seeded("writer"));
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Sprint 7");
    expect(screen.getByTestId("sprint-state")).toHaveAttribute("data-status", "active");
    expect(screen.getAllByTestId("board-column").map((one) => one.dataset.status)).toEqual(["backlog", "todo", "in_progress", "in_review", "done"]);
    expect(refsIn("in_review")).toEqual(["T-14"]);
    expect(refsIn("todo")).toEqual(["T-15"]);
    expect(screen.getByTestId("board-cancelled")).toBeInTheDocument();
    const card = within(column("in_review")).getByTestId("task-card");
    expect(within(card).getByTestId("task-codes")).toHaveTextContent("UC ORD-3");
    expect(within(card).getByTestId("task-assignee-select")).toHaveValue("__many__");
  });

  it("keeps the tasks of a role, an SRS code or the visitor's own", async () => {
    const user = userEvent.setup();
    withClient(<BoardPage project={PROJECT} title="Tasks" ask={null} initialError={null} />, seeded("writer"));
    await user.selectOptions(screen.getByTestId("board-code"), "UC ORD-3");
    expect(screen.getAllByTestId("task-card").map((card) => card.dataset.task)).toEqual(["T-15", "T-14"]);
    await user.selectOptions(screen.getByTestId("board-code"), "");
    await user.selectOptions(screen.getByTestId("board-role"), "ba");
    expect(screen.getAllByTestId("task-card").map((card) => card.dataset.task)).toEqual(["T-12"]);
    await user.selectOptions(screen.getByTestId("board-role"), "");
    await user.click(screen.getByTestId("board-mine"));
    expect(screen.getAllByTestId("task-card").map((card) => card.dataset.task)).toEqual(["T-15", "T-12", "T-14"]);
  });

  it("moves a card as soon as its status changes, and keeps it there once the hub agrees", async () => {
    const user = userEvent.setup();
    answer = (ref, change) => {
      const moved = { ...TASKS[3], ref, status: change.status ?? "todo", status_group: "review" as const };
      rows = TASKS.map((one) => (one.ref === ref ? moved : one));
      return json(detailOf(moved));
    };
    withClient(<BoardPage project={PROJECT} title="Tasks" ask={null} initialError={null} />, seeded("writer"));
    await user.selectOptions(within(column("todo")).getByTestId("task-status-select"), "in_review");
    await waitFor(() => expect(refsIn("in_review")).toEqual(["T-14", "T-15"]));
    await waitFor(() => expect(sent).toContainEqual({ method: "PATCH", path: "/v1/x/tasks/demo/tasks/T-15", body: { status: "in_review" } }));
    expect(await screen.findByText("Đã chuyển T-15 sang In review")).toBeInTheDocument();
    expect(refsIn("in_review")).toEqual(["T-14", "T-15"]);
    expect(refsIn("todo")).toEqual([]);
  });

  it("puts a card back and says why when the hub refuses the change", async () => {
    const user = userEvent.setup();
    answer = () => json({ code: "forbidden", message: "changing a task needs the writer role" }, 403);
    withClient(<BoardPage project={PROJECT} title="Tasks" ask={null} initialError={null} />, seeded("writer"));
    await user.selectOptions(within(column("todo")).getByTestId("task-assignee-select"), "chi");
    expect(await screen.findByText("Không đổi được T-15")).toBeInTheDocument();
    await waitFor(() => expect(within(column("todo")).getByTestId("task-assignee-select")).toHaveValue(""));
    expect(refsIn("todo")).toEqual(["T-15"]);
  });

  it("gives a reader the board with no control that writes", () => {
    withClient(<BoardPage project={PROJECT} title="Tasks" ask={null} initialError={null} />, seeded("reader"));
    expect(screen.getByTestId("tasks-read-only")).toBeInTheDocument();
    expect(screen.queryAllByTestId("task-status-select")).toHaveLength(0);
    expect(screen.queryByTestId("tasks-new")).not.toBeInTheDocument();
    expect(screen.getAllByTestId("task-card")[0]).toHaveAttribute("draggable", "false");
  });
});

describe("TaskLinks", () => {
  const link = (id: number, patch: Partial<TaskLink>): TaskLink => ({
    id,
    kind: "srs_item",
    hidden: false,
    code: null,
    item_kind: null,
    item_title: null,
    document: null,
    retired: false,
    plan_id: null,
    step: null,
    step_title: null,
    step_status: null,
    runs: [],
    run_id: null,
    run_kind: null,
    run_state: null,
    url: null,
    created_by: "alice",
    created_at: "2026-10-13T02:00:00Z",
    ...patch,
  });
  const task: TaskDetail = detailOf(TASKS[2], {
    links: [
      link(1, { code: "UC ORD-3", item_kind: "use_case", item_title: "Cancel an order", document: "demo-shop" }),
      link(2, { hidden: true }),
      link(3, { kind: "plan_step", plan_id: "order-cancel", step: "2", step_title: "Cancel window", step_status: "done", runs: [{ id: 431, state: "done" }] }),
      link(4, { kind: "pull", url: "https://github.com/acme/shop/pull/88" }),
    ],
  });

  it("shows each link as it is now, opens the SRS item and the plan step, and hides what the visitor cannot read", () => {
    withClient(<TaskLinks project={PROJECT} task={task} settings={SETTINGS} writer={false} />, seeded("reader"));
    expect(screen.getByRole("link", { name: "UC ORD-3" })).toHaveAttribute("href", "/p/demo/x/srs/documents/demo-shop/items/UC%20ORD-3");
    expect(screen.getByRole("link", { name: "order-cancel#2" })).toHaveAttribute("href", "/p/demo/plans/order-cancel/steps/2");
    expect(screen.getByText("acme/shop#88")).toBeInTheDocument();
    expect(screen.getAllByTestId("task-link").filter((one) => one.textContent?.includes("không có quyền đọc"))).toHaveLength(1);
    expect(screen.queryByTestId("task-link-form")).not.toBeInTheDocument();
    expect(screen.queryAllByTestId("task-link-remove")).toHaveLength(0);
  });

  it("refuses a link no target can be before asking the hub, then sends the ones it reads", async () => {
    const user = userEvent.setup();
    answer = () => json(task);
    withClient(<TaskLinks project={PROJECT} task={task} settings={SETTINGS} writer />, seeded("writer"));
    await user.type(screen.getByTestId("task-link-input"), "a#b#c");
    await user.click(screen.getByTestId("task-link-add"));
    expect(screen.getByText("Không phải liên kết: a#b#c")).toBeInTheDocument();
    expect(sent).toHaveLength(0);
    await user.clear(screen.getByTestId("task-link-input"));
    await user.type(screen.getByTestId("task-link-input"), "BR ORD3-2, #12");
    await user.click(screen.getByTestId("task-link-add"));
    await waitFor(() =>
      expect(sent).toContainEqual({ method: "PATCH", path: "/v1/x/tasks/demo/tasks/T-14", body: { links: { use_cases: ["BR ORD3-2"], steps: [], runs: [12], pulls: [] } } }),
    );
  });
});
