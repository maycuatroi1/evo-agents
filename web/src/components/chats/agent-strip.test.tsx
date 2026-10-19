// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TooltipProvider } from "@/components/ui/tooltip";
import { createApiClient } from "@/lib/api/client";
import { renderVi } from "@/test/render";

import { AgentStrip, nextIndex } from "./agent-strip";
import { projectOrder } from "./use-addable-agents";

const api = vi.hoisted(() => ({
  grants: [] as { project: string; role: string }[],
  agents: {} as Record<string, unknown[] | number>,
  direct: 200,
  seen: [] as { url: string; method: string; headers: Headers }[],
}));
const router = vi.hoisted(() => ({ push: vi.fn() }));

vi.mock("next/navigation", () => ({ useRouter: () => router, usePathname: () => "/chats", useParams: () => ({}) }));
vi.mock("@/components/shell/project-switcher", () => ({ useSelectedProject: () => "northwind" }));
vi.mock("@/lib/api/browser", () => ({
  browserApi: () =>
    createApiClient({
      baseUrl: "http://hub.test",
      fetch: async (request) => {
        const url = new URL(request.url);
        api.seen.push({ url: url.pathname, method: request.method, headers: request.headers });
        if (url.pathname === "/v1/auth/web/csrf") return Response.json({ csrf: "csrf-1", header: "X-Evo-CSRF" });
        if (url.pathname === "/v1/auth/whoami") return Response.json({ login: "octo", grants: api.grants });
        if (url.pathname === "/v1/features") return Response.json({ features: [], projects: [] });
        const agents = /^\/v1\/projects\/([^/]+)\/agents$/.exec(url.pathname);
        if (agents) {
          const answer = api.agents[agents[1]];
          return typeof answer === "number" ? Response.json({ error: { code: "server", message: "down" } }, { status: answer }) : Response.json(answer ?? []);
        }
        const direct = /^\/v1\/chats\/direct\/([^/]+)$/.exec(url.pathname);
        if (direct && request.method === "PUT") {
          if (api.direct !== 200) return Response.json({ error: { code: "forbidden", message: "not a writer of northwind" } }, { status: api.direct });
          return Response.json({ id: 9, title: "Code reviewer", kind: "direct", agents: [], people: [] });
        }
        throw new Error(`unexpected ${request.method} ${url.pathname}`);
      },
    }),
}));

function agent(project: string, handle: string, overrides: Record<string, unknown> = {}) {
  return { project, handle, name: handle[0].toUpperCase() + handle.slice(1), color: "#2563eb", icon_url: null, is_default: false, deleted_at: null, ...overrides };
}

function setup() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  renderVi(
    <QueryClientProvider client={client}>
      <TooltipProvider>
        <AgentStrip current="fixer" />
      </TooltipProvider>
    </QueryClientProvider>,
  );
}

// The tooltips measure their trigger, which jsdom cannot.
class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}

const handles = () => screen.queryAllByTestId("agent-strip-agent").map((item) => item.getAttribute("data-handle"));

afterEach(() => vi.unstubAllGlobals());

beforeEach(() => {
  vi.stubGlobal("ResizeObserver", ResizeObserverStub);
  api.grants = [
    { project: "lumen", role: "writer" },
    { project: "northwind", role: "admin" },
    { project: "archive", role: "reader" },
  ];
  api.agents = {
    lumen: [agent("lumen", "lumen", { is_default: true }), agent("lumen", "docs")],
    northwind: [agent("northwind", "reviewer"), agent("northwind", "northwind", { is_default: true }), agent("northwind", "gone", { deleted_at: "2026-10-01T00:00:00Z" }), agent("northwind", "fixer")],
    archive: [agent("archive", "archive", { is_default: true })],
  };
  api.direct = 200;
  api.seen = [];
  router.push.mockReset();
});

describe("the row of agents", () => {
  it("puts the project shown first, then the others by name, each default agent first, without deleted ones or projects only read", () => {
    expect(projectOrder(["northwind", "lumen", "archive"], "northwind")).toEqual(["northwind", "archive", "lumen"]);
    expect(projectOrder(["northwind", "lumen"], "elsewhere")).toEqual(["lumen", "northwind"]);
    expect(projectOrder(["northwind", "lumen"])).toEqual(["lumen", "northwind"]);
  });

  it("shows the agents with their names and marks the agent of the direct chat shown", async () => {
    setup();
    expect(screen.getByTestId("agent-strip")).toHaveAttribute("data-state", "loading");
    await waitFor(() => expect(handles()).toEqual(["northwind", "fixer", "reviewer", "lumen", "docs"]));
    const fixer = screen.getByRole("button", { name: "Chat với Fixer, @fixer của northwind" });
    expect(fixer).toHaveAttribute("aria-current", "true");
    expect(screen.getByRole("toolbar", { name: "Chat với một agent" })).toBeInTheDocument();
  });

  it("is one Tab stop that the arrows, Home and End move along", async () => {
    const user = userEvent.setup();
    setup();
    await waitFor(() => expect(handles()).toHaveLength(5));
    const buttons = screen.getAllByTestId("agent-strip-agent");
    expect(buttons.map((button) => button.tabIndex)).toEqual([0, -1, -1, -1, -1]);
    await user.tab();
    expect(buttons[0]).toHaveFocus();
    await user.keyboard("{ArrowRight}");
    expect(buttons[1]).toHaveFocus();
    expect(buttons.map((button) => button.tabIndex)).toEqual([-1, 0, -1, -1, -1]);
    await user.keyboard("{End}");
    expect(buttons[4]).toHaveFocus();
    await user.keyboard("{ArrowRight}");
    expect(buttons[0]).toHaveFocus();
    await user.keyboard("{ArrowLeft}");
    expect(buttons[4]).toHaveFocus();
    await user.keyboard("{Home}");
    expect(buttons[0]).toHaveFocus();
    expect([nextIndex("ArrowDown", 0, 5), nextIndex("Enter", 2, 5)]).toEqual([null, null]);
  });

  it("opens the visitor's direct chat with an agent on Enter, with the composer focused", async () => {
    const user = userEvent.setup();
    setup();
    await waitFor(() => expect(handles()).toHaveLength(5));
    await user.tab();
    await user.keyboard("{ArrowRight}{ArrowRight}{Enter}");
    await waitFor(() => expect(router.push).toHaveBeenCalledWith("/chats/9?compose=1"));
    const put = api.seen.find((call) => call.method === "PUT");
    expect(put?.url).toBe("/v1/chats/direct/reviewer");
    expect(put?.headers.get("X-Evo-CSRF")).toBe("csrf-1");
  });

  it("says when the hub refuses to open a chat, and nothing opens", async () => {
    const user = userEvent.setup();
    api.direct = 403;
    setup();
    await waitFor(() => expect(handles()).toHaveLength(5));
    await user.click(screen.getByRole("button", { name: /^Chat với Docs/ }));
    expect(await screen.findByText("Không mở được chat với @docs")).toBeInTheDocument();
    expect(router.push).not.toHaveBeenCalled();
  });

  it("names the projects whose agents could not be read and reads them again on Try again", async () => {
    const user = userEvent.setup();
    api.agents.lumen = 503;
    setup();
    await waitFor(() => expect(handles()).toEqual(["northwind", "fixer", "reviewer"]));
    expect(screen.getByTestId("agent-strip-failed")).toHaveTextContent("Không đọc được agent của lumen.");
    api.agents.lumen = [agent("lumen", "lumen", { is_default: true })];
    await user.click(screen.getByTestId("agent-strip-retry"));
    await waitFor(() => expect(handles()).toEqual(["northwind", "fixer", "reviewer", "lumen"]));
    expect(screen.queryByTestId("agent-strip-failed")).toBeNull();
  });

  it("says where the agents come from while there are none", async () => {
    api.grants = [{ project: "archive", role: "reader" }];
    setup();
    expect(await screen.findByTestId("agent-strip-empty")).toHaveTextContent("Agent của các dự án bạn có quyền ghi hiện ở đây để chat riêng.");
    expect(screen.getByTestId("agent-strip")).toHaveAttribute("data-state", "empty");
  });
});
