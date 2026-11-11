// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createApiClient } from "@/lib/api/client";
import { systemObserveHref } from "@/lib/systems-queries";
import { renderVi } from "@/test/render";

import type { PlanRunOutcome } from "./plan-run-choice";
import { PlanRunDialog } from "./plan-run-dialog";
import { planTraceHref } from "./plan-run-outcome";

const api = vi.hoisted(() => ({
  answer: null as null | ((request: Request) => Response | Promise<Response>),
  seen: [] as { method: string; path: string; body: unknown }[],
}));

vi.mock("@/lib/api/browser", () => ({
  browserApi: () =>
    createApiClient({
      baseUrl: "http://hub.test",
      fetch: async (request) => {
        const text = request.method === "GET" ? "" : await request.clone().text();
        api.seen.push({ method: request.method, path: new URL(request.url).pathname, body: text ? JSON.parse(text) : null });
        if (request.url.endsWith("/v1/auth/web/csrf")) return Response.json({ csrf: "csrf-1", header: "X-Evo-CSRF" });
        if (!api.answer) throw new Error("no answer set");
        return api.answer(request);
      },
    }),
}));

const json = (body: unknown, status = 200) => Response.json(body, { status, headers: { "x-request-id": "rid-9" } });

const PLAN = {
  project: "demo",
  plan_id: "rollout",
  area: "active",
  revision: 3,
  digest: "d",
  label: {},
  body: {
    id: "rollout",
    title: "Rollout",
    repos: [{ repo: "api", branch: "feat/rollout" }],
    steps: [
      { id: 1, title: "Model", repo: "api", status: "done" },
      { id: 2, title: "Queue", repo: "api", status: "pending" },
      { id: 3, title: "Deploy", repo: "api", status: "pending", depends_on: [2] },
      { id: 4, title: "Catalog", repo: "api", status: "pending", depends_on: [3] },
    ],
  },
  created_at: "2026-10-05T07:00:00Z",
  updated_at: "2026-10-05T07:00:00Z",
  updated_by: "octo",
};

const step = (key: string, title: string, status: string, ready: boolean) => ({
  key,
  title,
  repo: "api",
  status,
  ready,
  reason: ready ? null : "it waits for an earlier step",
  active_run: null,
});

const READY = {
  project: "demo",
  plan_id: "rollout",
  revision: 3,
  plan_run: null,
  trace: null,
  steps: [step("1", "Model", "done", false), step("2", "Queue", "pending", true), step("3", "Deploy", "pending", false), step("4", "Catalog", "pending", false)],
};

const summary = (slug: string, name: string, entry: string | null) => ({
  slug,
  name,
  revision: 2,
  entry_agent: entry,
  nodes: 2,
  edges: 1,
  updated_by: "octo",
  updated_at: "2026-10-05T07:00:00Z",
});

const SYSTEMS = [summary("lumen-review", "Lumen review", "lumen-lead"), summary("northwind-ops", "Northwind ops", "northwind-desk"), summary("lumen-draft", "Lumen draft", null)];

const SPECS: Record<string, object> = {
  "lumen-review": { schema: 1, name: "Lumen review", purpose: "Reviews and ships the rollout steps.", limits: { trace_usd: 1.5, trace_runs: 4, daily_usd: 5 }, nodes: [], edges: [] },
  "northwind-ops": { schema: 1, name: "Northwind ops", purpose: "", nodes: [], edges: [] },
};

const CHOICE = {
  target: "system",
  system: "lumen-review",
  agent: null,
  runtime: "any",
  model: null,
  mode: "headless",
  timeout_h: 8,
  session_per_step: false,
  saved: true,
  defaulted: ["model"],
  updated_at: "2026-10-05T07:00:00Z",
};

const DISPATCHED = {
  system: "lumen-review",
  system_revision: 2,
  trace_id: 77,
  plan_id: "rollout",
  plan_revision: 3,
  chat_id: 12,
  message_id: 340,
  run_id: 91,
};

type Options = { choice?: object | null; ready?: object; dispatch?: () => Response };

function answer({ choice = CHOICE, ready = READY, dispatch = () => json(DISPATCHED, 201) }: Options = {}) {
  return (request: Request) => {
    const path = new URL(request.url).pathname;
    if (path === "/v1/projects/demo/plans/rollout") return json(PLAN);
    if (path === "/v1/projects/demo/plans/rollout/ready-steps") return json(ready);
    if (path === "/v1/projects/demo") return json({ name: "demo", repos: [{ name: "api", default_branch: "main" }] });
    if (path === "/v1/auth/whoami") return json({ login: "octo", admin: false, token: {}, grants: [] });
    if (path === "/v1/workers") return json([]);
    if (path === "/v1/projects/demo/systems") return json(SYSTEMS);
    const one = /^\/v1\/projects\/demo\/systems\/([^/]+)$/.exec(path);
    if (one && SPECS[one[1]]) {
      const system = SYSTEMS.find((item) => item.slug === one[1]);
      return json({ project: "demo", ...system, latest_revision: 2, spec: SPECS[one[1]], pins: [], problems: [], can_change: true, created_by: "octo", created_at: "2026-10-05T07:00:00Z" });
    }
    if (path === "/v1/projects/demo/plan-run-choice") {
      if (request.method === "PUT") return json({ ...CHOICE, defaulted: [] });
      return choice ? json(choice) : json({ error: "not_found", message: path }, 404);
    }
    if (path === "/v1/projects/demo/plans/rollout/system-dispatch" && request.method === "POST") return dispatch();
    return json({ error: "not_found", message: path }, 404);
  };
}

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

function Harness({ onDispatched }: { onDispatched: (outcome: PlanRunOutcome) => void }) {
  const [open, setOpen] = useState(true);
  return (
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <PlanRunDialog project="demo" planId="rollout" open={open} onOpenChange={setOpen} onDispatched={onDispatched} />
      <p data-testid="open-state">{open ? "open" : "closed"}</p>
    </QueryClientProvider>
  );
}

const radioOf = (card: HTMLElement) => within(card).getByRole("radio") as HTMLInputElement;

describe("PlanRunDialog where it runs", () => {
  it("opens on the remembered system, shows each system's entry agent and limits, and hands the plan to it", async () => {
    const user = userEvent.setup();
    const dispatched = vi.fn();
    api.answer = answer();
    renderVi(<Harness onDispatched={dispatched} />);
    const dialog = screen.getByRole("dialog", { name: "Chạy plan" });

    const where = await within(dialog).findByTestId("plan-run-where");
    const review = within(where).getByTestId("plan-run-where-system-lumen-review");
    const ops = within(where).getByTestId("plan-run-where-system-northwind-ops");
    // Only the systems with an entry agent are offered.
    expect(within(where).queryByTestId("plan-run-where-system-lumen-draft")).toBeNull();
    expect(radioOf(review)).toBeChecked();
    expect(radioOf(within(where).getByTestId("plan-run-where-worker"))).not.toBeChecked();
    expect(within(dialog).getByTestId("plan-run-defaulted")).toHaveTextContent("Model");

    // Name, purpose, entry agent and limits; 3 pending steps need about 6 runs, over lumen-review's 4.
    expect(review).toHaveTextContent("Lumen review");
    await waitFor(() => expect(review).toHaveTextContent("Reviews and ships the rollout steps."));
    expect(review).toHaveTextContent("@lumen-lead");
    expect(review).toHaveTextContent(/4 run/);
    expect(within(review).getByTestId("plan-run-where-over")).toHaveTextContent(/3 bước.*6 run.*4 run/);
    // northwind-ops keeps the hub's default limits: 12 runs, no warning.
    await waitFor(() => expect(ops).toHaveTextContent(/12 run/));
    expect(within(ops).queryByTestId("plan-run-where-over")).toBeNull();

    // The worker's fields are hidden while a system is chosen.
    expect(within(dialog).queryByTestId("plan-run-timeout")).toBeNull();
    expect(within(dialog).queryByTestId("plan-run-model-select")).toBeNull();
    expect(within(dialog).queryByTestId("dispatch-runtime-claude-code")).toBeNull();

    const submit = within(dialog).getByTestId("plan-run-submit");
    await waitFor(() => expect(submit).toBeEnabled());
    await user.click(submit);

    await waitFor(() => expect(dispatched).toHaveBeenCalledTimes(1));
    expect(dispatched.mock.calls[0][0]).toMatchObject({ project: "demo", system: "lumen-review", trace_id: 77, run_id: 91 });
    expect(screen.getByTestId("open-state")).toHaveTextContent("closed");
    const post = api.seen.find((call) => call.method === "POST" && call.path.endsWith("/system-dispatch"));
    expect(post?.body).toEqual({ system: "lumen-review" });
    expect(api.seen.some((call) => call.method === "POST" && call.path.endsWith("/plan-runs"))).toBe(false);
    await waitFor(() => expect(api.seen.some((call) => call.method === "PUT")).toBe(true));
    const put = api.seen.find((call) => call.method === "PUT");
    expect(put?.path).toBe("/v1/projects/demo/plan-run-choice");
    expect(put?.body).toMatchObject({ target: "system", system: "lumen-review", timeout_h: 8, mode: "headless" });
    for (const key of ["as_me", "account", "identities", "browser"]) expect(put?.body).not.toHaveProperty(key);
  });

  it("starts on a worker and the dialog's defaults without a saved choice, and shows the worker's fields again when it is picked", async () => {
    const user = userEvent.setup();
    // The hub's defaults for a member who saved none: its timeout is the API's 4 hours, the dialog keeps its 24.
    api.answer = answer({ choice: { ...CHOICE, target: "worker", system: null, timeout_h: 4, saved: false, defaulted: [], updated_at: null } });
    renderVi(<Harness onDispatched={vi.fn()} />);
    const dialog = screen.getByRole("dialog", { name: "Chạy plan" });

    const where = await within(dialog).findByTestId("plan-run-where");
    expect(radioOf(within(where).getByTestId("plan-run-where-worker"))).toBeChecked();
    expect(within(dialog).queryByTestId("plan-run-defaulted")).toBeNull();
    expect(within(dialog).getByTestId("plan-run-timeout")).toHaveValue("24");

    await user.click(radioOf(within(where).getByTestId("plan-run-where-system-northwind-ops")));
    expect(within(dialog).queryByTestId("plan-run-timeout")).toBeNull();
    await user.click(radioOf(within(where).getByTestId("plan-run-where-worker")));
    expect(within(dialog).getByTestId("plan-run-timeout")).toBeInTheDocument();
  });

  it("names the trace that runs the plan and does not run it again", async () => {
    api.answer = answer({ ready: { ...READY, trace: { id: 77, system: "lumen-review", run_id: 91, state: "running" } } });
    renderVi(<Harness onDispatched={vi.fn()} />);
    const dialog = screen.getByRole("dialog", { name: "Chạy plan" });

    const blocker = await within(dialog).findByTestId("plan-run-blocker-trace");
    expect(blocker).toHaveTextContent("lumen-review");
    expect(within(blocker).getByRole("link", { name: "Trace #77" })).toHaveAttribute("href", "/p/demo/systems/lumen-review?mode=observe&work=77");
    await within(dialog).findByTestId("plan-run-where");
    expect(within(dialog).getByTestId("plan-run-submit")).toBeDisabled();
  });

  it("says why the hub did not hand the plan over", async () => {
    const user = userEvent.setup();
    const dispatched = vi.fn();
    api.answer = answer({ dispatch: () => json({ error: "conflict", message: "the plan has no pending step" }, 409) });
    renderVi(<Harness onDispatched={dispatched} />);
    const dialog = screen.getByRole("dialog", { name: "Chạy plan" });

    const submit = await within(dialog).findByTestId("plan-run-submit");
    await waitFor(() => expect(radioOf(within(dialog).getByTestId("plan-run-where-system-lumen-review"))).toBeChecked());
    await waitFor(() => expect(submit).toBeEnabled());
    await user.click(submit);
    expect(await within(dialog).findByText(/Hub không giao plan cho system/)).toBeInTheDocument();
    expect(within(dialog).getByText(/the plan has no pending step/)).toBeInTheDocument();
    expect(dispatched).not.toHaveBeenCalled();
    expect(api.seen.some((call) => call.method === "PUT")).toBe(false);
  });
});

describe("planTraceHref", () => {
  it("links to the trace as the system's Observe does", () => {
    expect(planTraceHref("demo", "lumen-review", 77)).toBe(systemObserveHref("demo", "lumen-review", 77));
    expect(planTraceHref("demo", "a b", 3)).toBe(systemObserveHref("demo", "a b", 3));
  });
});
