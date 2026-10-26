// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { Agent } from "@/lib/agents";
import { createApiClient } from "@/lib/api/client";
import { queryKeys } from "@/lib/queries";
import { renderVi } from "@/test/render";

import { AgentWorkersPanel } from "./agent-workers-panel";

const api = vi.hoisted(() => ({
  agent: null as unknown,
  patch: null as null | ((request: Request) => Response | Promise<Response>),
  seen: [] as Request[],
}));

vi.mock("@/lib/api/browser", () => ({
  browserApi: () =>
    createApiClient({
      baseUrl: "http://hub.test",
      fetch: async (request) => {
        api.seen.push(request);
        const url = new URL(request.url);
        if (url.pathname === "/v1/auth/web/csrf") return Response.json({ csrf: "csrf-1", header: "X-Evo-CSRF" });
        if (url.pathname === "/v1/projects/demo/agents/reviewer" && request.method === "PATCH" && api.patch) return api.patch(request);
        if (url.pathname === "/v1/projects/demo/agents/reviewer") return Response.json(api.agent);
        if (url.pathname === "/v1/workers") return Response.json([worker("vps-1", "vps"), worker("desk", null)]);
        if (url.pathname === "/v1/worker-pools") return Response.json([{ name: "vps" }, { name: "spare" }]);
        if (["/v1/secrets", "/v1/skills", "/v1/projects"].includes(url.pathname)) return Response.json([]);
        return Response.json({ error: "not_found", message: url.pathname }, { status: 404 });
      },
    }),
}));

function worker(name: string, pool: string | null) {
  return { name, owner: "octo", status: "online", labels: pool ? [`pool=${pool}`] : [], pool, runtimes: {} };
}

const AGENT: Agent = {
  handle: "reviewer",
  project: "demo",
  name: "Reviewer",
  description: "",
  instructions: "",
  color: "#2563eb",
  icon_url: null,
  avatar_sha256: null,
  is_default: false,
  permissions: {},
  restricted: [],
  packs: [],
  version: 3,
  created_by: "octo",
  created_at: "2026-10-14T03:00:00Z",
  updated_at: "2026-10-14T03:00:00Z",
  deleted_at: null,
};

function setup(role: "admin" | "writer") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  client.setQueryData(queryKeys.whoami, { login: "octo", admin: false, token: {}, grants: [{ project: "demo", role, max_level: "internal" }] });
  renderVi(
    <QueryClientProvider client={client}>
      <AgentWorkersPanel project="demo" handle="reviewer" />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  api.agent = AGENT;
  api.patch = null;
  api.seen = [];
});

describe("where an agent's runs may go, from a chat", () => {
  it("lets an admin limit it to a pool, saved as a new version against the one shown", async () => {
    const user = userEvent.setup();
    let body: unknown = null;
    api.patch = async (request) => {
      body = await request.json();
      api.agent = { ...AGENT, version: 4, permissions: { worker: { mode: "allowlist", allow: ["pool:vps"] } } };
      return Response.json(api.agent);
    };
    setup("admin");
    const panel = await screen.findByTestId("agent-workers");
    expect(panel).toHaveAttribute("data-editable", "true");
    expect(within(panel).getByTestId("agent-workers-save")).toBeDisabled();
    await user.click(within(panel).getByTestId("agent-axis-worker-allowlist"));
    const options = await within(panel).findByTestId("agent-axis-worker-options");
    expect(within(options).getAllByTestId("agent-axis-worker-option").map((box) => box.getAttribute("data-value"))).toEqual(["desk", "vps-1", "pool:spare", "pool:vps"]);
    expect(within(options).getByText("1 node bạn thấy")).toBeInTheDocument();
    await user.click(within(options).getByRole("checkbox", { name: /pool:vps/ }));
    await user.click(within(panel).getByTestId("agent-workers-save"));
    await waitFor(() => expect(body).not.toBeNull());
    const sent = body as { permissions: Record<string, unknown>; if_version: number };
    expect(sent.if_version).toBe(3);
    expect(sent.permissions.worker).toEqual({ mode: "allowlist", allow: ["pool:vps"] });
    expect(sent.permissions.credential).toEqual({ mode: "all" });
    expect(await screen.findByText("Đã lưu nơi @reviewer chạy")).toBeInTheDocument();
  });

  it("says an edit made meanwhile was refused, and keeps saying it once the agent as it is now shows", async () => {
    const user = userEvent.setup();
    api.patch = () => {
      api.agent = { ...AGENT, version: 4, permissions: { worker: { mode: "allowlist", allow: ["desk"] } } };
      return Response.json({ error: "conflict", message: "agent @reviewer is at version 4 now" }, { status: 409 });
    };
    setup("admin");
    const panel = await screen.findByTestId("agent-workers");
    await user.click(within(panel).getByTestId("agent-axis-worker-allowlist"));
    await user.click(within(await within(panel).findByTestId("agent-axis-worker-options")).getByRole("checkbox", { name: /pool:vps/ }));
    await user.click(within(panel).getByTestId("agent-workers-save"));
    expect(await screen.findByText("Agent này vừa được người khác sửa. Bảng đã hiện bản mới nhất: hãy chọn lại.")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId("agent-axis-worker-options")).toBeInTheDocument());
    await waitFor(() => expect(screen.getByRole("checkbox", { name: /desk/ })).toBeChecked());
    expect(screen.getByText("Agent này vừa được người khác sửa. Bảng đã hiện bản mới nhất: hãy chọn lại.")).toBeInTheDocument();
  });

  it("shows a writer what the axis allows, read only", async () => {
    api.agent = { ...AGENT, permissions: { worker: { mode: "allowlist", allow: ["pool:vps", "label:gpu"] } } };
    setup("writer");
    const panel = await screen.findByTestId("agent-workers");
    expect(panel).toHaveAttribute("data-editable", "false");
    expect(within(panel).getByTestId("agents-read-only")).toBeInTheDocument();
    const summary = within(panel).getByTestId("agent-workers-summary");
    expect(within(summary).getAllByRole("listitem").map((item) => item.textContent)).toEqual(["pool:vps", "label:gpu"]);
    expect(within(panel).queryByTestId("agent-workers-save")).toBeNull();
  });

  it("tells an admin the project's default agent keeps every permission", async () => {
    api.agent = { ...AGENT, handle: "demo", is_default: true };
    setup("admin");
    const panel = await screen.findByTestId("agent-workers");
    expect(within(panel).getByTestId("agent-workers-fixed")).toHaveTextContent("giữ mọi quyền");
    expect(within(panel).getByTestId("agent-workers-summary")).toHaveTextContent("Mọi worker của người giao run");
  });
});
