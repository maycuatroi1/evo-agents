// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { createApiClient } from "@/lib/api/client";
import { renderVi } from "@/test/render";

import type { Run } from "./queries";
import { RunTriage } from "./run-triage";

/** A failed run's page: the cause (and whether the hub guessed it), the triage thread and the candidates. */

const api = vi.hoisted(() => ({ status: 200, body: {} as unknown, asked: 0 }));

vi.mock("@/lib/api/browser", () => ({
  browserApi: () =>
    createApiClient({
      baseUrl: "http://hub.test",
      fetch: async (request) => {
        const path = new URL(request.url).pathname;
        if (path === "/v1/projects/demo/runs/12/rerun-candidates") {
          api.asked += 1;
          return Response.json(api.body, { status: api.status });
        }
        return Response.json({ error: "not_found", message: path }, { status: 404 });
      },
    }),
}));

const run = { id: 12, project: "demo", kind: "step", state: "failed", failure_cause: "verify_failed", dispatched_by: "octo" } as unknown as Run;

const candidates = {
  run_id: 12,
  project: "demo",
  kind: "step",
  state: "failed",
  failure_cause: "verify_failed",
  cause_inferred: false,
  triage: { chat_id: 5, root_message_id: 9, link: "/chats/5?message=9" },
  workers: [
    { worker_id: "w1", worker: "mac-1", fit: true, rank: 1, reasons: ["has pnpm"], is_run_worker: false, online: true, free_slots: 1, runtimes: [], models: [], labels: {}, accounts: [] },
    { worker_id: "w2", worker: "mac-2", fit: false, rank: null, reasons: ["offline"], is_run_worker: true, online: false, free_slots: 0, runtimes: [], models: [], labels: {}, accounts: [] },
  ],
  agents: [{ profile_id: 1, handle: "triager", name: "Triager", is_default: true, fit: true, rank: 1, reasons: [], project: "demo" }],
  accounts: [],
  other_projects: [],
};

function show(owner: boolean, value: Run = run) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return renderVi(
    <QueryClientProvider client={client}>
      <RunTriage run={value} owner={owner} />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  api.status = 200;
  api.body = candidates;
  api.asked = 0;
});

describe("RunTriage", () => {
  it("shows the cause, a link to the thread and the candidates for the owner", async () => {
    show(true);
    const link = await screen.findByTestId("run-triage-thread");
    expect(link).toHaveAttribute("href", "/chats/5?message=9");
    expect(screen.getByTestId("run-triage-cause")).toHaveTextContent("verify_failed");
    expect(screen.queryByTestId("run-triage-guessed")).toBeNull();
    const summary = await screen.findByTestId("run-triage-summary");
    expect(summary).toHaveTextContent("1/2");
    const workers = screen.getAllByTestId("run-triage-worker");
    expect(workers).toHaveLength(2);
    expect(within(workers[0]).getByTestId("run-triage-fit")).toHaveAttribute("data-fit", "true");
    expect(within(workers[1]).getByTestId("run-triage-fit")).toHaveAttribute("data-fit", "false");
    expect(screen.getAllByTestId("run-triage-agent")).toHaveLength(1);
  });

  it("says when the hub guessed the cause", async () => {
    api.body = { ...candidates, cause_inferred: true };
    show(true);
    expect(await screen.findByTestId("run-triage-guessed")).toBeInTheDocument();
  });

  it("says so when no thread was opened", async () => {
    api.body = { ...candidates, triage: null };
    show(true);
    expect(await screen.findByTestId("run-triage-no-thread")).toBeInTheDocument();
    expect(screen.queryByTestId("run-triage-thread")).toBeNull();
  });

  it("asks the hub for no candidates when the visitor is not the owner, and keeps the cause", () => {
    show(false);
    expect(screen.getByTestId("run-triage-cause")).toHaveTextContent("verify_failed");
    expect(screen.queryByTestId("run-triage-candidates")).toBeNull();
    expect(api.asked).toBe(0);
  });

  it("names a push the origin declined by its own line, not as another cause", () => {
    show(false, { ...run, failure_cause: "push_declined" } as Run);
    expect(screen.getByTestId("run-triage-cause")).toHaveTextContent("push_declined");
    expect(screen.getByText("Push bị từ chối")).toBeInTheDocument();
    expect(screen.queryByText("Nguyên nhân khác")).toBeNull();
  });

  it("renders nothing for a run that did not fail or has no triage", () => {
    const { container } = show(true, { ...run, state: "done" } as Run);
    expect(container.querySelector("[data-testid=run-triage]")).toBeNull();
    expect(api.asked).toBe(0);
  });

  it("names a candidates failure without hiding the cause", async () => {
    api.status = 409;
    api.body = { error: "conflict", message: "not failed" };
    show(true);
    expect(await screen.findByTestId("run-triage-error")).toBeInTheDocument();
    expect(screen.getByTestId("run-triage-cause")).toBeInTheDocument();
  });
});
