// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { createApiClient } from "@/lib/api/client";
import { renderVi } from "@/test/render";

import { DispatchDialog } from "./dispatch-dialog";
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

const plan = (id: string, area: "active" | "completed", title: string | null) => ({
  plan_id: id,
  area,
  revision: 3,
  digest: "d",
  title,
  steps_total: 4,
  steps_done: 1,
  updated_at: "2026-10-05T07:00:00Z",
  updated_by: "octo",
});

const worker = (id: number, name: string, owner: string, extra: Record<string, unknown> = {}) => ({
  id,
  name,
  owner,
  status: "online",
  hostname: `${name}.local`,
  os: "Linux",
  arch: "x86_64",
  agent_version: "0.3.0",
  slots: 2,
  labels: [],
  projects: ["demo"],
  runtimes: { "claude-code": { available: true, version: "2.1.289" } },
  checkouts: { "demo/api": { path: "~/github/api", branch: "main" } },
  free_slots: 2,
  allow_web_terminal: false,
  dispatch_from: "any",
  held_runs: 0,
  created_at: "2026-10-05T07:00:00Z",
  last_heartbeat_at: new Date().toISOString(),
  drained_at: null,
  revoked_at: null,
  ...extra,
});

const READY = {
  project: "demo",
  plan_id: "rollout",
  revision: 3,
  steps: [
    { key: "1", title: "Model", repo: "api", status: "done", ready: false, reason: "its status is done, not pending", active_run: null },
    { key: "2", title: "Queue", repo: "api", status: "pending", ready: true, reason: null, active_run: null },
    { key: "3", title: "Daemon", repo: "api", status: "pending", ready: false, reason: "it waits for step 2 (pending)", active_run: null },
    { key: "4", title: "Docs", repo: "api", status: "pending", ready: true, reason: null, active_run: null },
    {
      key: "5",
      title: "Web",
      repo: "api",
      status: "pending",
      ready: false,
      reason: "it has run #40, review, dispatched by octo",
      active_run: { id: 40, state: "review", dispatched_by: "octo" },
    },
  ],
};

function queued(id: number, step: string): Run {
  return { id, project: "demo", plan_id: "rollout", step_key: step, state: "queued" } as Run;
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

function answer(post: (request: Request) => Response | Promise<Response>) {
  return (request: Request) => {
    const url = new URL(request.url);
    if (url.pathname === "/v1/projects/demo/plans") return json([plan("rollout", "active", "Rollout"), plan("old", "completed", null)]);
    if (url.pathname === "/v1/auth/whoami") return json({ login: "octo", admin: false, token: {}, grants: [] });
    if (url.pathname === "/v1/workers") return json([worker(7, "lab-ws-01", "octo"), worker(8, "theirs", "someone-else")]);
    if (url.pathname === "/v1/projects/demo/plans/rollout/ready-steps") return json(READY);
    if (url.pathname === "/v1/projects/demo/runs" && request.method === "POST") return post(request);
    return json({ error: "not_found", message: url.pathname }, 404);
  };
}

function Harness({ onDispatched }: { onDispatched: (runs: Run[]) => void }) {
  const [open, setOpen] = useState(true);
  return (
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <DispatchDialog project="demo" open={open} onOpenChange={setOpen} onDispatched={onDispatched} plan="rollout" step="4" />
      <p data-testid="open-state">{open ? "open" : "closed"}</p>
    </QueryClientProvider>
  );
}

describe("DispatchDialog", () => {
  it("offers the ready steps only, fills the worker that can take them, and dispatches with the CSRF header", async () => {
    const user = userEvent.setup();
    const dispatched = vi.fn();
    api.answer = answer(() => json([queued(41, "4"), queued(42, "2")], 201));
    renderVi(<Harness onDispatched={dispatched} />);

    const dialog = screen.getByRole("dialog", { name: "Dispatch run" });
    // Only active plans are offered; the step the dialog was opened on is picked once it shows as ready.
    const planSelect = await within(dialog).findByLabelText("Plan");
    await waitFor(() => expect(within(planSelect).getAllByRole("option").map((option) => option.textContent)).toEqual(["rollout: Rollout"]));
    const docs = await within(dialog).findByRole("checkbox", { name: /Docs/ });
    await waitFor(() => expect(docs).toBeChecked());

    // A step that is not ready shows why and cannot be picked; the done step is folded away but still listed.
    const daemon = within(dialog).getByRole("checkbox", { name: /Daemon/ });
    expect(daemon).toBeDisabled();
    expect(within(dialog).getByTestId("dispatch-step-3")).toHaveTextContent("It waits for step 2 (pending)");
    expect(within(dialog).getByTestId("dispatch-step-5")).toHaveTextContent("Run #40 đang ở trạng thái Chờ duyệt, do octo dispatch");
    expect(within(dialog).getByTestId("dispatch-settled")).toHaveTextContent("1 bước đã xong, đang làm hoặc bị chặn");
    expect(within(dialog).getByRole("checkbox", { name: /Model/ })).toBeDisabled();
    expect(within(dialog).getByTestId("dispatch-step-1")).toHaveTextContent("Trạng thái là Xong, không phải chưa làm");

    // Only the visitor's own worker is offered, and filled in since it can take the runs now.
    const workerSelect = within(dialog).getByTestId("dispatch-worker-select") as HTMLSelectElement;
    await waitFor(() => expect(workerSelect.value).toBe("7"));
    expect(within(workerSelect).getAllByRole("option").map((option) => option.textContent)).toEqual(["lab-ws-01: còn 2 slot trống", "Worker bất kỳ phù hợp"]);
    expect(within(dialog).queryByTestId("dispatch-outlook")).toBeNull();
    await user.click(within(dialog).getByRole("checkbox", { name: /Queue/ }));
    expect(within(dialog).getByRole("button", { name: "Dispatch 2 run" })).toBeEnabled();

    // A runtime the worker lacks: the worker cannot be picked, and below the dropdown it says why.
    const codex = within(dialog).getByRole("radio", { name: "Codex CLI" });
    expect(codex).toHaveAccessibleDescription("Chưa worker nào có");
    await user.click(codex);
    expect(within(workerSelect).getByRole("option", { name: "lab-ws-01: không có Codex CLI" })).toBeDisabled();
    expect(workerSelect.value).toBe("any");
    expect(within(dialog).getByTestId("dispatch-outlook")).toHaveTextContent("lab-ws-01: không có Codex CLI");
    await user.click(within(dialog).getByRole("radio", { name: "Claude Code" }));
    expect(workerSelect.value).toBe("7");
    expect(within(dialog).queryByTestId("dispatch-outlook")).toBeNull();
    expect(within(dialog).getByTestId("dispatch-approval-info")).toHaveAccessibleDescription(/Giữ lại chờ duyệt: bạn đọc diff và log/);
    await user.click(within(dialog).getByRole("radio", { name: "Đánh dấu xong" }));
    await user.selectOptions(within(dialog).getByLabelText("Thời gian tối đa mỗi run"), "120");
    await user.click(within(dialog).getByRole("button", { name: "Dispatch 2 run" }));

    await waitFor(() => expect(screen.getByTestId("open-state")).toHaveTextContent("closed"));
    const post = api.seen.find((request) => request.method === "POST");
    expect(post?.headers.get("X-Evo-CSRF")).toBe("csrf-1");
    expect(await post?.clone().json()).toEqual({
      plan_id: "rollout",
      steps: ["4", "2"],
      runtime: "claude-code",
      mode: "headless",
      approval: "auto",
      timeout_min: 120,
      worker_id: 7,
      as_me: false,
    });
    expect(dispatched).toHaveBeenCalledWith([queued(41, "4"), queued(42, "2")]);
  });

  it("lets the runs act as the visitor on the hub only when asked, and then says the risk", async () => {
    const user = userEvent.setup();
    api.answer = answer(() => json([queued(41, "4")], 201));
    renderVi(<Harness onDispatched={vi.fn()} />);
    const dialog = screen.getByRole("dialog", { name: "Dispatch run" });
    await waitFor(() => expect(within(dialog).getByRole("checkbox", { name: /Docs/ })).toBeChecked());
    const asMe = within(dialog).getByRole("checkbox", { name: /Chạy như tôi trên hub/ });
    expect(asMe).not.toBeChecked();
    expect(asMe).toHaveAccessibleDescription(/không bao giờ chạm tới secrets, tokens hay workers của bạn/);
    expect(within(dialog).queryByTestId("dispatch-as-me-warning")).toBeNull();
    await user.click(asMe);
    expect(within(dialog).getByTestId("dispatch-as-me-warning")).toHaveTextContent("Bạn thu hồi được bất cứ lúc nào trên trang của run.");
    await user.click(within(dialog).getByRole("button", { name: "Dispatch 1 run" }));
    await waitFor(() => expect(screen.getByTestId("open-state")).toHaveTextContent("closed"));
    expect((await api.seen.find((request) => request.method === "POST")?.clone().json())?.as_me).toBe(true);
  });

  it("keeps the dialog open and says why when the hub refuses a step that stopped being ready", async () => {
    const user = userEvent.setup();
    const dispatched = vi.fn();
    api.answer = answer(() => json({ error: "conflict", message: "step 4 of plan rollout is not ready", request_id: "rid-7" }, 409));
    renderVi(<Harness onDispatched={dispatched} />);
    const dialog = screen.getByRole("dialog", { name: "Dispatch run" });
    await waitFor(() => expect(within(dialog).getByRole("checkbox", { name: /Docs/ })).toBeChecked());
    await user.click(within(dialog).getByRole("button", { name: "Dispatch 1 run" }));
    const alert = await within(dialog).findByRole("alert");
    expect(alert).toHaveTextContent("Một bước không còn sẵn sàng");
    expect(alert).toHaveTextContent("Hub báo: step 4 of plan rollout is not ready");
    expect(screen.getByTestId("open-state")).toHaveTextContent("open");
    expect(dispatched).not.toHaveBeenCalled();
    expect((await api.seen.find((request) => request.method === "POST")?.clone().json())?.worker_id).toBe(7);
  });

  it("fills the ready account for the runtime, dispatches with it, and says why when the hub refuses it", async () => {
    const user = userEvent.setup();
    const accounts = [
      { name: "claude-b", env_var: "CLAUDE_CODE_OAUTH_TOKEN", projects: ["demo"], workers: [], paused_at: null, state: "ready" },
      { name: "elsewhere", env_var: "CLAUDE_CODE_OAUTH_TOKEN", projects: ["other"], workers: [], paused_at: null, state: "ready" },
    ];
    const reason = "account claude-b is paused: resume it, or pick another account; nothing was dispatched";
    let refuse = true;
    const base = answer(() => (refuse ? json({ error: "unprocessable", message: reason, request_id: "rid-7" }, 422) : json([queued(41, "4")], 201)));
    api.answer = (request) => (new URL(request.url).pathname === "/v1/accounts" ? json(accounts) : base(request));
    renderVi(<Harness onDispatched={vi.fn()} />);
    const dialog = screen.getByRole("dialog", { name: "Dispatch run" });
    await waitFor(() => expect(within(dialog).getByRole("checkbox", { name: /Docs/ })).toBeChecked());
    const select = within(dialog).getByTestId("dispatch-account-select") as HTMLSelectElement;
    // The ready account bound to the project, for Claude Code's variable, is filled in; the rules stay a choice.
    await waitFor(() => expect((within(select).getAllByRole("option") as HTMLOptionElement[]).map((option) => option.value)).toEqual(["", "claude-b"]));
    expect(select.value).toBe("claude-b");
    await user.click(within(dialog).getByRole("button", { name: "Dispatch 1 run" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(`Hub báo: ${reason}`);
    refuse = false;
    await user.click(within(dialog).getByRole("button", { name: "Dispatch 1 run" }));
    await waitFor(() => expect(screen.getByTestId("open-state")).toHaveTextContent("closed"));
    const posts = api.seen.filter((request) => request.method === "POST" && request.url.endsWith("/runs"));
    expect((await posts.at(-1)?.clone().json())?.account).toBe("claude-b");
  });

  it("dispatches under the agent picked, narrowed to what it allows", async () => {
    const user = userEvent.setup();
    const agent = (handle: string, permissions: Record<string, unknown>, extra: Record<string, unknown> = {}) => ({
      handle,
      project: "demo",
      name: handle === "demo" ? "demo" : `Agent ${handle}`,
      description: "",
      instructions: "",
      color: "#2563eb",
      icon_url: null,
      avatar_sha256: null,
      is_default: handle === "demo",
      permissions,
      restricted: Object.keys(permissions),
      version: 2,
      created_by: "octo",
      created_at: "2026-10-14T03:00:00Z",
      updated_at: "2026-10-14T03:00:00Z",
      deleted_at: null,
      ...extra,
    });
    const agents = [
      agent("demo", {}),
      agent("reviewer", { runtime: { mode: "allowlist", allow: ["claude-code"] }, worker: { mode: "allowlist", allow: ["lab-ws-01"] } }),
      agent("gpu", { worker: { mode: "allowlist", allow: ["label:gpu"] } }),
      agent("gone", {}, { deleted_at: "2026-10-14T04:00:00Z" }),
    ];
    const base = answer(() => json([queued(41, "4")], 201));
    api.answer = (request) => (new URL(request.url).pathname === "/v1/projects/demo/agents" ? json(agents) : base(request));
    renderVi(<Harness onDispatched={vi.fn()} />);
    const dialog = screen.getByRole("dialog", { name: "Dispatch run" });
    await waitFor(() => expect(within(dialog).getByRole("checkbox", { name: /Docs/ })).toBeChecked());

    // The project's agents that take runs, the default first and picked.
    const select = (await within(dialog).findByTestId("dispatch-agent-select")) as HTMLSelectElement;
    expect((within(select).getAllByRole("option") as HTMLOptionElement[]).map((option) => option.value)).toEqual(["demo", "gpu", "reviewer"]);
    expect(select.value).toBe("demo");
    expect(within(dialog).getByTestId("dispatch-agent-info")).toHaveAccessibleDescription(/^Agent mặc định của dự án/);

    // An agent that leaves out every worker of the visitor's: said, and nothing goes out.
    await user.selectOptions(select, "gpu");
    expect(within(dialog).getByTestId("dispatch-agent-problem")).toHaveTextContent("@gpu không cho worker nào của bạn ở đây nhận run: nó chỉ cho label:gpu.");
    expect(within(dialog).getByTestId("dispatch-agent-blocked")).toHaveTextContent("@gpu từ chối lần giao này");
    expect(within(dialog).getByRole("button", { name: "Dispatch 1 run" })).toBeDisabled();

    // A runtime the agent leaves out, then one it allows.
    await user.selectOptions(select, "reviewer");
    expect(within(dialog).getByTestId("dispatch-agent-info")).toHaveAccessibleDescription("Nó giới hạn worker ở lab-ws-01 và runtime ở Claude Code.");
    await user.click(within(dialog).getByRole("radio", { name: /^Codex CLI/ }));
    expect(within(dialog).getByTestId("dispatch-agent-problem")).toHaveTextContent("@reviewer không cho phép Codex CLI: nó chỉ cho Claude Code.");
    await user.click(within(dialog).getByRole("radio", { name: /^Claude Code/ }));
    expect(within(dialog).queryByTestId("dispatch-agent-problem")).toBeNull();
    await user.click(within(dialog).getByRole("button", { name: "Dispatch 1 run" }));
    await waitFor(() => expect(screen.getByTestId("open-state")).toHaveTextContent("closed"));
    const post = api.seen.find((request) => request.method === "POST" && request.url.endsWith("/runs"));
    expect(await post?.clone().json()).toMatchObject({ profile: "reviewer", runtime: "claude-code", steps: ["4"] });
  });

  it("cannot dispatch before a ready step is picked", async () => {
    api.answer = answer(() => json([], 201));
    renderVi(<Harness onDispatched={vi.fn()} />);
    const dialog = screen.getByRole("dialog", { name: "Dispatch run" });
    const docs = await within(dialog).findByRole("checkbox", { name: /Docs/ });
    await waitFor(() => expect(docs).toBeChecked());
    await userEvent.setup().click(docs);
    expect(within(dialog).getByRole("button", { name: "Dispatch" })).toBeDisabled();
    expect(within(dialog).queryByTestId("dispatch-outlook")).toBeNull();
  });
});
