// @vitest-environment jsdom
import { screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { renderVi } from "@/test/render";

import type { Run } from "./queries";
import { RunsTable } from "./runs-table";

vi.mock("next/navigation", () => ({
  usePathname: () => "/p/demo/runs",
  useParams: () => ({ project: "demo" }),
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), prefetch: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
}));

const TASK = {
  id: 77,
  project: "demo",
  kind: "task",
  profile: "builder",
  plan_id: "",
  step_key: null,
  title: "Add a CSV export",
  state: "running",
  worker_id: null,
  worker: null,
  pinned_worker_id: null,
  dispatched_by: "plugin:builder",
  runtime: "claude",
  attempt: 1,
  max_attempts: 1,
  repos: [],
  branch: "task/77",
  error: null,
  created_at: "2026-10-18T07:00:00Z",
  started_at: "2026-10-18T07:00:05Z",
  finished_at: null,
} as unknown as Run;

// A lost run's next attempt is a new run on the branch of the run the dispatch made.
const RETRY = { ...TASK, id: 81, attempt: 2, max_attempts: 2, parent_run_id: 77 } as unknown as Run;

describe("RunsTable, a task run", () => {
  it("carries the Task run tag, says the plugin handed the task to its agent on the run's branch, and links to the run's page", () => {
    renderVi(<RunsTable runs={[TASK]} caption="Runs" viewer={null} />);
    const table = screen.getByTestId("runs-table");
    const kinds = within(table).getAllByTestId("run-kind");
    expect(kinds.length).toBeGreaterThan(0);
    for (const kind of kinds) {
      expect(kind).toHaveAttribute("data-kind", "task");
      expect(kind).toHaveTextContent("Task run");
    }
    expect(within(table).getAllByText("Một task plugin giao cho @builder, trên nhánh task/77").length).toBeGreaterThan(0);
    for (const link of within(table).getAllByRole("link", { name: "Add a CSV export" })) {
      expect(link).toHaveAttribute("href", "/p/demo/runs/77");
    }
  });

  it("names the branch the hub recorded for a later attempt, not one of its own id", () => {
    renderVi(<RunsTable runs={[RETRY]} caption="Runs" viewer={null} />);
    const table = screen.getByTestId("runs-table");
    expect(within(table).getAllByText("Một task plugin giao cho @builder, trên nhánh task/77").length).toBeGreaterThan(0);
    expect(within(table).queryByText(/task\/81/)).toBeNull();
    for (const link of within(table).getAllByRole("link", { name: "Add a CSV export" })) {
      expect(link).toHaveAttribute("href", "/p/demo/runs/81");
    }
  });
});

const PLAN = {
  ...TASK,
  id: 90,
  kind: "plan",
  plan_id: "model-providers",
  title: "model-providers",
  plan_progress: { percent: 30, done: 3, total: 10, current_step: "Run dispatch trên model của provider", current_status: "in_progress" },
} as unknown as Run;

describe("RunsTable, a plan run's progress", () => {
  it("draws the share of done steps as a bar, with the count and the current step on one truncated line", () => {
    renderVi(<RunsTable runs={[PLAN]} caption="Runs" viewer={null} />);
    const table = screen.getByTestId("runs-table");
    const progress = within(table).getAllByTestId("run-progress")[0];
    const bar = within(progress).getByRole("progressbar", { name: "Tiến độ plan" });
    expect(bar).toHaveAttribute("aria-valuenow", "3");
    expect(bar).toHaveAttribute("aria-valuemax", "10");
    expect((bar.firstElementChild as HTMLElement).style.width).toBe("30%");
    expect(within(progress).getByTestId("run-progress-count")).toHaveTextContent("3/10");
    const step = within(progress).getByTestId("run-progress-step");
    expect(step).toHaveTextContent("Run dispatch trên model của provider: in_progress");
    expect(step).toHaveClass("truncate");
    expect(step).toHaveAttribute("title", "Run dispatch trên model của provider: in_progress");
  });
});
