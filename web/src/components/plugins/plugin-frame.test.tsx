// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { createApiClient } from "@/lib/api/client";
import type { PluginPage, ProjectPlugin } from "@/lib/plugin-queries";
import { renderVi } from "@/test/render";

import { BRIDGE } from "./bridge";
import { PluginFrame } from "./plugin-frame";

const api = vi.hoisted(() => ({ calls: [] as string[] }));

vi.mock("@/lib/api/browser", () => ({
  browserApi: () =>
    createApiClient({
      baseUrl: "http://hub.test",
      fetch: async (request) => {
        const url = new URL(request.url);
        api.calls.push(url.pathname);
        if (url.pathname === "/v1/auth/whoami") {
          return Response.json({ login: "octo", admin: false, grants: [{ project: "demo", role: "reader" }] });
        }
        if (url.pathname === "/v1/projects/demo/runs") return Response.json({ runs: [], total: 0 });
        return Response.json({ message: "not here" }, { status: 404 });
      },
    }),
}));

const PAGE: PluginPage = {
  name: "board",
  title: "Board",
  description: "",
  icon: "kanban",
  menu: true,
  native: false,
  url: "/v1/plugin-ui/sample/demo/1.abc/index.html",
};

const PLUGIN = {
  name: "sample",
  title: "Sample",
  granted: ["runs:read"],
  pages: [PAGE],
} as unknown as ProjectPlugin;

function setup() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  renderVi(
    <QueryClientProvider client={client}>
      <PluginFrame project="demo" plugin={PLUGIN} page={PAGE} />
    </QueryClientProvider>,
  );
  const frame = screen.getByTestId("plugin-frame") as HTMLIFrameElement;
  const posted = vi.fn();
  Object.defineProperty(frame.contentWindow, "postMessage", { value: posted, configurable: true });
  return { frame, posted };
}

function send(source: MessageEventSource | null, data: unknown, origin = "null") {
  window.dispatchEvent(new MessageEvent("message", { data, origin, source }));
}

beforeEach(() => {
  api.calls = [];
});

describe("PluginFrame", () => {
  it("frames the page in a sandbox without the same origin, once the bridge listens", () => {
    const { frame } = setup();
    expect(frame.getAttribute("sandbox")).toBe("allow-scripts allow-forms");
    expect(frame.getAttribute("src")).toBe(PAGE.url);
    expect(frame.getAttribute("referrerpolicy")).toBe("no-referrer");
    expect(frame).toHaveAccessibleName("Board, một trang của plugin Sample");
  });

  it("answers its own frame's opaque origin through the hub, and refuses what the project did not grant", async () => {
    const { frame, posted } = setup();
    send(frame.contentWindow, { bridge: BRIDGE, id: 1, method: "runs.list", params: { limit: 5 } });
    await waitFor(() => expect(posted).toHaveBeenCalledWith({ bridge: BRIDGE, id: 1, result: { runs: [], total: 0 } }, "*"));
    expect(api.calls).toContain("/v1/projects/demo/runs");
    send(frame.contentWindow, { bridge: BRIDGE, id: 2, method: "plans.list" });
    await waitFor(() =>
      expect(posted).toHaveBeenCalledWith(
        expect.objectContaining({ id: 2, error: expect.objectContaining({ code: "not_granted" }) }),
        "*",
      ),
    );
    expect(api.calls.some((path) => path.includes("/plans"))).toBe(false);
  });

  it("ignores a message from another window or from an origin that is not opaque", async () => {
    const { frame, posted } = setup();
    send(window, { bridge: BRIDGE, id: 3, method: "context" });
    send(frame.contentWindow, { bridge: BRIDGE, id: 4, method: "context" }, "http://hub.test");
    send(frame.contentWindow, { type: "not the bridge's" });
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(posted).not.toHaveBeenCalled();
    send(frame.contentWindow, { bridge: BRIDGE, id: 5, method: "context" });
    await waitFor(() =>
      expect(posted).toHaveBeenCalledWith(
        expect.objectContaining({ id: 5, result: expect.objectContaining({ project: "demo", plugin: "sample", page: "board" }) }),
        "*",
      ),
    );
  });
});
