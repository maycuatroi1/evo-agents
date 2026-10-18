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
  error: null,
  created_at: "2026-10-18T07:00:00Z",
  started_at: "2026-10-18T07:00:05Z",
  finished_at: null,
} as unknown as Run;

describe("RunsTable, a task run", () => {
  it("carries the Task run tag, says the plugin handed the task to its agent on branch task/<run>, and links to the run's page", () => {
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
});
