// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createApiClient } from "@/lib/api/client";
import { renderVi } from "@/test/render";

import { AuthorRunDialog } from "./author-run-dialog";
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


const PLAN = {
  project: "demo",
  plan_id: "rollout",
  area: "active",
  revision: 3,
  digest: "d",
  label: {},
  body: { id: "rollout", title: "Worker fleet rollout", steps: [] },
  created_at: "2026-10-05T07:00:00Z",
  updated_at: "2026-10-05T07:00:00Z",
  updated_by: "octo",
};

function answer(post: (request: Request) => Response | Promise<Response>, harness: object | null = { name: "demo", workspace: "~/work", path: "harness" }) {
  return (request: Request) => {
    const url = new URL(request.url);
    if (url.pathname === "/v1/projects/demo/plans/rollout") return json(PLAN);
    if (url.pathname === "/v1/projects/demo") return json({ name: "demo", repos: [{ name: "api", default_branch: "main" }], harness });
    if (url.pathname === "/v1/auth/whoami") return json({ login: "octo", admin: false, token: {}, grants: [] });
    if (url.pathname === "/v1/workers") {
      return json([worker(9, "laptop", "octo", { checkouts: { "demo/api": { path: "~/api" } } }), worker(7, "lab-ws-01", "octo"), worker(8, "theirs", "someone-else")]);
    }
    if (url.pathname === "/v1/projects/demo/author-runs" && request.method === "POST") return post(request);
    return json({ error: "not_found", message: url.pathname }, 404);
  };
}

function Harness({ planId = null, onDispatched }: { planId?: string | null; onDispatched: (run: Run) => void }) {
  const [open, setOpen] = useState(true);
  return (
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <AuthorRunDialog project="demo" planId={planId} open={open} onOpenChange={setOpen} onDispatched={onDispatched} />
      <p data-testid="open-state">{open ? "open" : "closed"}</p>
    </QueryClientProvider>
  );
}

describe("AuthorRunDialog", () => {
  it("takes the request, picks a worker with the harness, and queues a new plan's author run with the CSRF header", async () => {
    const user = userEvent.setup();
    const dispatched = vi.fn();
    const queued = { id: 61, kind: "author", project: "demo", plan_id: "", state: "queued" } as Run;
    api.answer = answer(() => json(queued, 201));
    renderVi(<Harness onDispatched={dispatched} />);

    const dialog = screen.getByRole("dialog", { name: "Plan mới" });
    expect(dialog).toHaveAttribute("data-mode", "new");
    expect(within(dialog).getByTestId("author-run-runtime")).toHaveTextContent("Claude Code");
    // The worker with a checkout of the harness is picked first; the one without is said to fail to take it.
    const select = await within(dialog).findByTestId("author-run-worker-select");
    await waitFor(() => expect(select).toHaveValue("7"));
    expect(within(dialog).getByTestId("author-run-worker")).toHaveTextContent("harness");
    expect(within(dialog).getByTestId("dispatch-outlook")).toHaveTextContent("lab-ws-01 nhận được author run này ngay.");
    await user.selectOptions(select, "9");
    expect(within(dialog).getByTestId("dispatch-outlook")).toHaveTextContent("laptop chưa nhận được author run này (không có checkout của harness)");
    await user.selectOptions(select, "7");

    // A blank request is refused here, with the reason next to the field.
    await user.click(within(dialog).getByTestId("author-run-submit"));
    expect(within(dialog).getByTestId("author-run-request-problem")).toHaveTextContent("Hãy nói plan cần đạt được gì trước.");
    expect(api.seen.some((request) => request.method === "POST" && request.url.endsWith("/author-runs"))).toBe(false);

    await user.type(within(dialog).getByTestId("author-run-request"), "Let members write plans from the web.");
    await user.type(within(dialog).getByTestId("plan-run-model-input"), "opus");
    await user.selectOptions(within(dialog).getByLabelText("Thời gian tối đa"), "4");
    await user.click(within(dialog).getByTestId("author-run-submit"));

    await waitFor(() => expect(screen.getByTestId("open-state")).toHaveTextContent("closed"));
    const post = api.seen.find((request) => request.method === "POST" && request.url.endsWith("/author-runs"));
    expect(post?.headers.get("X-Evo-CSRF")).toBe("csrf-1");
    expect(await post?.clone().json()).toEqual({
      request: "Let members write plans from the web.",
      worker_id: 7,
      plan_id: null,
      runtime: "claude-code",
      model: "opus",
      timeout_h: 4,
    });
    expect(dispatched).toHaveBeenCalledWith(queued);
  });

  it("revises a plan by its id, and keeps the dialog open with the hub's reason when the hub refuses", async () => {
    const user = userEvent.setup();
    const dispatched = vi.fn();
    api.answer = answer(() => json({ error: "conflict", message: "worker lab-ws-01 runs no author run: upgrade its daemon" }, 409));
    renderVi(<Harness planId="rollout" onDispatched={dispatched} />);
    const dialog = screen.getByRole("dialog", { name: "Sửa cùng agent" });
    await waitFor(() => expect(within(dialog).getByText("Worker fleet rollout")).toBeInTheDocument());
    await user.type(within(dialog).getByTestId("author-run-request"), "Split step 4.");
    await waitFor(() => expect(within(dialog).getByTestId("author-run-submit")).toBeEnabled());
    await user.click(within(dialog).getByTestId("author-run-submit"));
    const alert = await within(dialog).findByTestId("admin-dialog-error");
    expect(alert).toHaveTextContent("Hub không xếp author run vào queue");
    expect(alert).toHaveTextContent("Hub báo: worker lab-ws-01 runs no author run");
    expect(screen.getByTestId("open-state")).toHaveTextContent("open");
    expect(dispatched).not.toHaveBeenCalled();
    expect(await api.seen.find((request) => request.method === "POST")?.clone().json()).toMatchObject({ plan_id: "rollout", worker_id: 7, model: null, timeout_h: 2 });
  });

  it("says when the project has no harness to write in, and offers no run", async () => {
    api.answer = answer(() => json({}, 201), null);
    renderVi(<Harness onDispatched={vi.fn()} />);
    const dialog = screen.getByRole("dialog", { name: "Plan mới" });
    expect(await within(dialog).findByTestId("author-run-no-harness")).toHaveTextContent("được đăng ký mà không có harness");
    expect(within(dialog).getByTestId("author-run-submit")).toBeDisabled();
  });
});
