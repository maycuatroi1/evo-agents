// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { renderVi } from "@/test/render";

import { type PmDaily, pmKeys, validDate } from "./api";
import { DailyPage } from "./daily-page";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  usePathname: () => "/p/demo/x/pm/daily",
  useSearchParams: () => new URLSearchParams(),
}));

const PROJECT = "demo";
const NOW = "2026-10-20T03:00:00Z";

const BLOCKER = {
  ref: "T-3",
  title: "Pay by card",
  kind: "manual",
  reason: "Waiting for the gateway keys",
  waiting_kind: "text",
  waiting_on: "Acme bank",
  since: NOW,
  set_by: "alice",
  age_days: 3,
  decision_id: null,
  run_id: null,
} as const;

const DAILY: PmDaily = {
  project: PROJECT,
  date: "2026-10-21",
  previous_working_day: "2026-10-20",
  done_use_cases: [{ code: "UC ORD-1", title: "Place an order", task: "T-1", done_at: NOW, outcome: "Orders are placed from the cart." }],
  done_tasks: [{ ref: "T-2", title: "Cart totals", assignees: ["bao"], done_at: NOW }],
  sprint: {
    name: "Sprint 7",
    starts_on: "2026-10-12",
    ends_on: "2026-10-23",
    opened_at: NOW,
    scope: 8,
    done_percent: 50,
    added: 2,
    removed: 1,
    working_days: 10,
    working_days_gone: 7,
    time_percent: 70,
    use_cases: [{ code: "UC ORD-1", title: "Place an order", done: 2, total: 4 }],
  },
  late: [{ ref: "T-4", title: "Refund flow", assignees: ["chi"], due_on: "2026-10-19", kind: "overdue", days_late: 2, days_left: null }],
  review: [{ ref: "T-5", title: "Order e-mail", assignees: ["bao"], qa: ["chi"], since: NOW, age_days: 1 }],
  stale: [{ ref: "T-6", title: "Search", assignees: ["bao"], last_activity_at: NOW, source: "history", idle_days: 3 }],
  blocked: [BLOCKER],
  escalate: [{ ...BLOCKER, escalate_to: "alice" }],
  load: [
    { login: "bao", role: "writer", doing: 3, wip_limit: 2, overloaded: true, empty: false, review_as_qa: 0, late: 0, doing_tasks: ["T-6", "T-7", "T-8"] },
    { login: "chi", role: "writer", doing: 0, wip_limit: 2, overloaded: false, empty: true, review_as_qa: 1, late: 1, doing_tasks: [] },
  ],
  hidden: { done_use_cases: 0, done_tasks: 0, sprint: 0, late: 2, review: 0, stale: 0, blocked: 0, escalate: 0 },
};

function show(daily: PmDaily | null, error?: { status: number; message: string }) {
  const client = new QueryClient({ defaultOptions: { queries: { staleTime: Infinity, retry: false } } });
  if (daily) client.setQueryData(pmKeys.daily(PROJECT, null), daily);
  const initialError = error ? { status: error.status, code: "conflict", message: error.message, requestId: null } : null;
  renderVi(
    <QueryClientProvider client={client}>
      <DailyPage project={PROJECT} title="PM daily" initialError={initialError} />
    </QueryClientProvider>,
  );
}

describe("DailyPage", () => {
  it("shows the eight sections, each task linking to its page in the tasks plugin", () => {
    show(DAILY);
    for (const id of ["done", "sprint", "late", "review", "stale", "blocked", "escalate", "load"]) {
      expect(screen.getByTestId(`pm-${id}`), id).toBeInTheDocument();
    }
    const late = screen.getByTestId("pm-late");
    expect(within(late).getByTestId("pm-task-link")).toHaveAttribute("href", "/p/demo/x/tasks/board/T-4");
    expect(within(screen.getByTestId("pm-done")).getByText("Orders are placed from the cart.")).toBeInTheDocument();
    expect(within(screen.getByTestId("pm-sprint")).getAllByTestId("pm-sprint-use-case")).toHaveLength(1);
  });

  it("names who a blocker waits on and its age, and counts what the reader may not see", () => {
    show(DAILY);
    const row = screen.getByTestId("pm-blocked-row");
    expect(row).toHaveTextContent("Acme bank");
    expect(within(row).getByTestId("pm-blocker-age")).toHaveTextContent("3 ngày làm việc");
    expect(screen.getByTestId("pm-late-hidden")).toBeInTheDocument();
    expect(screen.queryByTestId("pm-review-hidden")).toBeNull();
  });

  it("marks who is overloaded and who is free", () => {
    show(DAILY);
    const [bao, chi] = screen.getAllByTestId("pm-load-row");
    expect(within(bao).getByTestId("pm-overloaded")).toBeInTheDocument();
    expect(within(chi).getByTestId("pm-free")).toBeInTheDocument();
  });

  it("says what is empty without hiding the section", () => {
    show({ ...DAILY, sprint: null, blocked: [], escalate: [] });
    expect(screen.getByTestId("pm-sprint-empty")).toBeInTheDocument();
    expect(screen.getByTestId("pm-blocked-empty")).toBeInTheDocument();
  });

  it("explains a project with no task at all", () => {
    show({
      ...DAILY,
      done_use_cases: [],
      done_tasks: [],
      sprint: null,
      late: [],
      review: [],
      stale: [],
      blocked: [],
      escalate: [],
      load: [],
      hidden: { done_use_cases: 0, done_tasks: 0, sprint: 0, late: 0, review: 0, stale: 0, blocked: 0, escalate: 0 },
    });
    expect(screen.getByTestId("pm-bare")).toBeInTheDocument();
    expect(screen.queryByTestId("pm-report")).toBeNull();
  });

  it("says what to do when the hub answers 409", () => {
    show(null, { status: 409, message: "tasks is not enabled in this project" });
    expect(screen.getByTestId("pm-not-ready")).toBeInTheDocument();
    expect(screen.getByTestId("pm-not-ready-reason")).toHaveTextContent("tasks is not enabled in this project");
  });

  it("shows a loading state until the report arrives", () => {
    show(null);
    expect(screen.getByTestId("state-loading")).toBeInTheDocument();
  });
});

describe("validDate", () => {
  it("takes a day and nothing else", () => {
    expect(validDate("2026-10-21")).toBe("2026-10-21");
    expect(validDate("21/10/2026")).toBeNull();
    expect(validDate("2026-13-40")).toBeNull();
    expect(validDate(null)).toBeNull();
  });
});
