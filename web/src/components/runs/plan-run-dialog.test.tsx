// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createApiClient } from "@/lib/api/client";
import { renderVi } from "@/test/render";

import { PlanRunDialog } from "./plan-run-dialog";
import type { Run } from "./queries";

const api = vi.hoisted(() => ({
  answer: null as null | ((request: Request) => Response | Promise<Response>),
  seen: [] as Request[],
}));

vi.mock("@/lib/api/browser", () => ({
  browserApi: () =>
    createApiClient({
      baseUrl: "http://hub.test",
      fetch: async (request) => {
        api.seen.push(request);
        if (request.url.endsWith("/v1/auth/web/csrf")) return Response.json({ csrf: "csrf-1", header: "X-Evo-CSRF" });
        if (!api.answer) throw new Error("no answer set");
        return api.answer(request);
      },
    }),
}));

const json = (body: unknown, status = 200) => Response.json(body, { status, headers: { "x-request-id": "rid-7" } });

const BODY = {
  id: "rollout",
  title: "Rollout",
  repos: [
    { repo: "api", branch: "feat/rollout" },
    { repo: "harness", branch: "main" },
  ],
  steps: [
    { id: 1, title: "Model", repo: "api", status: "done" },
    { id: 2, title: "Queue", repo: "api", status: "pending" },
    { id: 3, title: "Checkpoint deploy", repo: "api", status: "pending", depends_on: [2] },
    { id: 4, title: "Catalog", repo: "harness", status: "pending", depends_on: [3] },
  ],
};

const PLAN = {
  project: "demo",
  plan_id: "rollout",
  area: "active",
  revision: 3,
  digest: "d",
  label: {},
  body: BODY,
  created_at: "2026-10-05T07:00:00Z",
  updated_at: "2026-10-05T07:00:00Z",
  updated_by: "octo",
};

const step = (key: string, title: string, repo: string, status: string, ready: boolean) => ({
  key,
  title,
  repo,
  status,
  ready,
  reason: ready ? null : status === "done" ? "its status is done, not pending" : "it waits for an earlier step",
  active_run: null,
});

const READY = {
  project: "demo",
  plan_id: "rollout",
  revision: 3,
  plan_run: null,
  steps: [
    step("1", "Model", "api", "done", false),
    step("2", "Queue", "api", "pending", true),
    step("3", "Checkpoint deploy", "api", "pending", false),
    step("4", "Catalog", "harness", "pending", false),
  ],
};

const worker = (id: number, name: string, owner: string, extra: Record<string, unknown> = {}) => ({
  id,
  name,
  owner,
  status: "online",
  hostname: `${name}.local`,
  os: "Linux",
  arch: "x86_64",
  agent_version: "0.4.0",
  slots: 1,
  labels: [],
  projects: ["demo"],
  runtimes: {
    "claude-code": { available: true, version: "2.1.289", models: ["opus", "sonnet"] },
    opencode: { available: true, version: "1.18.34", models: null },
  },
  checkouts: { "demo/api": { path: "~/github/api", branch: "main" }, "demo/harness": { path: "~/github/harness", branch: "main" } },
  free_slots: 1,
  allow_web_terminal: false,
  dispatch_from: "any",
  held_runs: 0,
  created_at: "2026-10-05T07:00:00Z",
  last_heartbeat_at: new Date().toISOString(),
  drained_at: null,
  revoked_at: null,
  ...extra,
});

class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}

beforeEach(() => {
  vi.stubGlobal("ResizeObserver", ResizeObserverStub);
  api.answer = null;
  api.seen = [];
});
afterEach(() => vi.unstubAllGlobals());

const WORKERS = [worker(7, "lab-ws-01", "octo"), worker(9, "laptop", "octo", { checkouts: { "demo/api": { path: "~/api" } } }), worker(8, "theirs", "someone-else")];

function answer(post: (request: Request) => Response | Promise<Response>, ready: object = READY, workers: object[] = WORKERS) {
  return (request: Request) => {
    const url = new URL(request.url);
    if (url.pathname === "/v1/projects/demo/plans/rollout") return json(PLAN);
    if (url.pathname === "/v1/projects/demo/plans/rollout/ready-steps") return json(ready);
    if (url.pathname === "/v1/projects/demo") return json({ name: "demo", repos: [{ name: "api", default_branch: "main" }, { name: "harness", default_branch: "main" }] });
    if (url.pathname === "/v1/auth/whoami") return json({ login: "octo", admin: false, token: {}, grants: [] });
    if (url.pathname === "/v1/workers") {
      return json(workers);
    }
    if (url.pathname === "/v1/projects/demo/plan-runs" && request.method === "POST") return post(request);
    return json({ error: "not_found", message: url.pathname }, 404);
  };
}

function Harness({ onDispatched }: { onDispatched: (run: Run) => void }) {
  const [open, setOpen] = useState(true);
  return (
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <PlanRunDialog project="demo" planId="rollout" open={open} onOpenChange={setOpen} onDispatched={onDispatched} />
      <p data-testid="open-state">{open ? "open" : "closed"}</p>
    </QueryClientProvider>
  );
}

describe("PlanRunDialog", () => {
  it("says what the run does, fills the worker it finds, offers the workers' models, and queues the plan run with the CSRF header", async () => {
    const user = userEvent.setup();
    const dispatched = vi.fn();
    const queued = { id: 51, kind: "plan", project: "demo", plan_id: "rollout", state: "queued" } as Run;
    api.answer = answer(() => json(queued, 201));
    renderVi(<Harness onDispatched={dispatched} />);

    const dialog = screen.getByRole("dialog", { name: "Chạy plan" });
    expect(dialog).toHaveAccessibleDescription("rollout");
    // Every step not done, the repos of those steps on the plan's branches, the checkpoint, and where the agent merges.
    const summary = await within(dialog).findByTestId("plan-run-summary");
    expect(dialog).toHaveAccessibleDescription("Rollout");
    expect(within(summary).getByTestId("plan-run-steps")).toHaveTextContent("3 bước sẽ chạy");
    expect(within(summary).getByTestId("plan-run-steps-info")).toHaveAccessibleDescription(/theo thứ tự depends_on/);
    expect(within(summary).getByTestId("plan-run-repo-api")).toHaveTextContent("feat/rollout");
    expect(within(summary).getByTestId("plan-run-repo-api")).not.toHaveTextContent("Nhánh mặc định");
    expect(within(summary).getByTestId("plan-run-repo-harness")).toHaveTextContent("mainNhánh mặc định");
    expect(within(summary).getByTestId("plan-run-checkpoints")).toHaveTextContent("3Checkpoint deploy");
    expect(within(summary).getByTestId("plan-run-push")).toHaveTextContent("Plan ghi nhánh mặc định của harness (main)");

    // The dropdown holds the first worker that can take the run now; one without a checkout of a repo says so.
    const workerSelect = within(dialog).getByTestId("dispatch-worker-select") as HTMLSelectElement;
    await waitFor(() => expect(workerSelect.value).toBe("7"));
    const laptop = within(workerSelect).getByRole("option", { name: "laptop: không có checkout của harness" });
    expect(laptop).toBeDisabled();
    expect(within(workerSelect).getByRole("option", { name: "Worker bất kỳ phù hợp" })).toBeInTheDocument();
    expect(within(dialog).queryByTestId("dispatch-outlook")).toBeNull();

    // Each runtime has its mark; a model of a runtime's list picks that runtime too.
    expect(within(dialog).getByTestId("dispatch-runtime-claude-code").querySelector("svg[data-runtime='claude-code']")).not.toBeNull();
    const modelSelect = within(dialog).getByTestId("plan-run-model-select") as HTMLSelectElement;
    const group = within(modelSelect).getByRole("group", { name: "Claude Code" });
    expect(within(group).getAllByRole("option").map((option) => option.textContent)).toEqual(["opus", "sonnet"]);
    await user.selectOptions(modelSelect, within(group).getByRole("option", { name: "sonnet" }));
    expect(within(dialog).getByRole("radio", { name: "Claude Code" })).toBeChecked();

    // Any runtime clears the model; a model typed with any runtime needs one picked.
    await user.click(within(dialog).getByRole("radio", { name: "Bất kỳ" }));
    expect(modelSelect.value).toBe("");
    await user.selectOptions(modelSelect, "Model khác…");
    await user.type(within(dialog).getByTestId("plan-run-model-input"), "sonnet");
    expect(within(dialog).getByTestId("plan-run-model-error")).toHaveTextContent("Chọn runtime cho model này");
    expect(within(dialog).getByTestId("plan-run-submit")).toBeDisabled();
    await user.click(within(dialog).getByRole("radio", { name: "Claude Code" }));
    expect(within(dialog).queryByTestId("plan-run-model-error")).toBeNull();

    const timeout = within(dialog).getByTestId("plan-run-timeout") as HTMLSelectElement;
    expect(timeout.value).toBe("24");
    await user.selectOptions(timeout, "8");
    await user.click(within(dialog).getByTestId("plan-run-submit"));

    await waitFor(() => expect(screen.getByTestId("open-state")).toHaveTextContent("closed"));
    const post = api.seen.find((request) => request.method === "POST");
    expect(post?.headers.get("X-Evo-CSRF")).toBe("csrf-1");
    expect(await post?.clone().json()).toEqual({
      plan_id: "rollout",
      worker_id: 7,
      runtime: "claude-code",
      model: "sonnet",
      mode: "headless",
      timeout_h: 8,
    });
    expect(dispatched).toHaveBeenCalledWith(queued);
  });

  it("leaves the run to any worker that fits when none can take it now, and says why below the dropdown", async () => {
    const user = userEvent.setup();
    const busy = [worker(7, "lab-ws-01", "octo", { held_runs: 1 })];
    api.answer = answer(() => json({ id: 54, kind: "plan", project: "demo", plan_id: "rollout", state: "queued" }, 201), READY, busy);
    renderVi(<Harness onDispatched={vi.fn()} />);
    const dialog = screen.getByRole("dialog", { name: "Chạy plan" });
    await within(dialog).findByTestId("plan-run-summary");
    const workerSelect = within(dialog).getByTestId("dispatch-worker-select") as HTMLSelectElement;
    await waitFor(() => expect(within(workerSelect).getByRole("option", { name: "lab-ws-01: hết slot" })).toBeInTheDocument());
    expect(workerSelect.value).toBe("any");
    expect(within(dialog).getByTestId("dispatch-outlook")).toHaveTextContent("lab-ws-01 (hết slot) nhận được plan run này sau");
    await user.selectOptions(workerSelect, "7");
    expect(within(dialog).getByTestId("dispatch-outlook")).toHaveTextContent("Plan run nằm trong queue tới khi lab-ws-01 có slot trống.");
  });

  it("cannot queue a plan run while the plan has one, and says which", async () => {
    api.answer = answer(() => json({}, 201), { ...READY, plan_run: { id: 50, state: "waiting", dispatched_by: "octo" } });
    renderVi(<Harness onDispatched={vi.fn()} />);
    const dialog = screen.getByRole("dialog", { name: "Chạy plan" });
    const blocker = await within(dialog).findByTestId("plan-run-blocker-planRun");
    expect(blocker).toHaveTextContent("Plan run #50 đang ở trạng thái Chờ quyết định, do octo giao");
    expect(within(blocker).getByRole("link", { name: "Plan run #50" })).toHaveAttribute("href", "/p/demo/runs/50");
    expect(within(dialog).getByTestId("plan-run-submit")).toBeDisabled();
  });

  it("keeps the dialog open with the hub's reason when the hub refuses", async () => {
    const user = userEvent.setup();
    const dispatched = vi.fn();
    api.answer = answer(() => json({ error: "conflict", message: "plan rollout has plan run #52, queued, dispatched by octo; nothing was dispatched" }, 409));
    renderVi(<Harness onDispatched={dispatched} />);
    const dialog = screen.getByRole("dialog", { name: "Chạy plan" });
    await within(dialog).findByTestId("plan-run-summary");
    await waitFor(() => expect(within(dialog).getByTestId("plan-run-submit")).toBeEnabled());
    await waitFor(() => expect((within(dialog).getByTestId("dispatch-worker-select") as HTMLSelectElement).value).toBe("7"));
    await user.click(within(dialog).getByTestId("plan-run-submit"));
    const alert = await within(dialog).findByTestId("admin-dialog-error");
    expect(alert).toHaveTextContent("Hub không đưa plan run vào queue");
    expect(alert).toHaveTextContent("Hub báo: plan rollout has plan run #52");
    expect(screen.getByTestId("open-state")).toHaveTextContent("open");
    expect(dispatched).not.toHaveBeenCalled();
    expect(await api.seen.find((request) => request.method === "POST")?.clone().json()).toMatchObject({ worker_id: 7, model: null, timeout_h: 24 });
  });

  it("runs each step in a new session only when switched on, keeps the workers that run that, and sends it only then", async () => {
    const user = userEvent.setup();
    const queued = { id: 53, kind: "plan", project: "demo", plan_id: "rollout", state: "queued", session_per_step: true } as Run;
    const workers = [worker(7, "lab-ws-01", "octo", { features: ["session_per_step"] }), worker(9, "old-box", "octo", { features: [] })];
    api.answer = answer(() => json(queued, 201), READY, workers);
    renderVi(<Harness onDispatched={vi.fn()} />);
    const dialog = screen.getByRole("dialog", { name: "Chạy plan" });
    await within(dialog).findByTestId("plan-run-summary");
    const sessions = await within(dialog).findByTestId("plan-run-sessions"); // its code loads when the dialog opens
    const toggle = within(sessions).getByRole("switch", { name: "Mỗi bước một phiên mới" });
    expect(toggle).toHaveAttribute("aria-checked", "false");
    expect(within(sessions).getByTestId("plan-run-sessions-info")).toHaveAccessibleDescription(/Tắt: một phiên agent làm mọi bước/);
    // Both workers take a plan run in one session.
    const workerSelect = within(dialog).getByTestId("dispatch-worker-select") as HTMLSelectElement;
    await waitFor(() => expect(within(workerSelect).getByRole("option", { name: "old-box: còn 1 slot trống" })).toBeEnabled());

    await user.click(toggle);
    expect(toggle).toHaveAttribute("aria-checked", "true");
    expect(within(sessions).queryByTestId("plan-run-sessions-reach")).toBeNull();
    expect(within(workerSelect).getByRole("option", { name: "old-box: daemon của nó chỉ chạy một phiên cho mọi bước" })).toBeDisabled();
    expect(workerSelect.value).toBe("7");
    await user.click(within(dialog).getByTestId("plan-run-submit"));
    await waitFor(() => expect(screen.getByTestId("open-state")).toHaveTextContent("closed"));
    expect(await api.seen.find((request) => request.method === "POST")?.clone().json()).toEqual({
      plan_id: "rollout",
      worker_id: 7,
      runtime: "any",
      model: null,
      mode: "headless",
      timeout_h: 24,
      session_per_step: true,
    });
  });

  it("says when none of the workers runs a session for each step", async () => {
    const user = userEvent.setup();
    api.answer = answer(() => json({}, 201), READY, [worker(9, "old-box", "octo", { features: [] })]);
    renderVi(<Harness onDispatched={vi.fn()} />);
    const dialog = screen.getByRole("dialog", { name: "Chạy plan" });
    const sessions = await within(dialog).findByTestId("plan-run-sessions");
    await waitFor(() => expect((within(dialog).getByTestId("dispatch-worker-select") as HTMLSelectElement).value).toBe("9"));
    await user.click(within(sessions).getByRole("switch", { name: "Mỗi bước một phiên mới" }));
    expect(within(sessions).getByTestId("plan-run-sessions-reach")).toHaveTextContent("Chưa worker nào của bạn cho dự án này chạy được");
    expect((within(dialog).getByTestId("dispatch-worker-select") as HTMLSelectElement).value).toBe("any");
  });
});
