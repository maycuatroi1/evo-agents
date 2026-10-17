// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { createApiClient } from "@/lib/api/client";
import { type PluginPage, type ProjectPlugin, pluginKeys } from "@/lib/plugin-queries";
import { renderVi } from "@/test/render";

import { BRIDGE } from "./bridge";
import { GRANT_FRESH_MS, PluginFrame } from "./plugin-frame";

// The hub as the frame reads it: each call's path and the plugin it names, and the plugin as the hub holds it now
// (null once it is removed), which a test changes as a project or a hub admin would while the page is open.
const api = vi.hoisted(() => ({
  calls: [] as string[],
  named: [] as (string | null)[],
  plugin: null as Record<string, unknown> | null,
}));

vi.mock("@/lib/api/browser", () => ({
  browserApi: () =>
    createApiClient({
      baseUrl: "http://hub.test",
      fetch: async (request) => {
        const url = new URL(request.url);
        api.calls.push(url.pathname);
        api.named.push(request.headers.get("X-Evo-Plugin"));
        if (url.pathname === "/v1/auth/whoami") {
          return Response.json({ login: "octo", admin: false, grants: [{ project: "demo", role: "reader" }] });
        }
        if (url.pathname === "/v1/projects/demo/plugins/sample" && api.plugin) return Response.json(api.plugin);
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
  enabled: true,
  state: "installed",
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
  return { frame, posted, client };
}

function send(source: MessageEventSource | null, data: unknown, origin = "null") {
  window.dispatchEvent(new MessageEvent("message", { data, origin, source }));
}

beforeEach(() => {
  api.calls = [];
  api.named = [];
  api.plugin = { ...PLUGIN };
});

function refused(posted: ReturnType<typeof vi.fn>, id: number, code: string) {
  return waitFor(() =>
    expect(posted).toHaveBeenCalledWith(expect.objectContaining({ id, error: expect.objectContaining({ code }) }), "*"),
  );
}

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
    // the grant read again first, then the runs, with the plugin named so the hub checks its grant too
    const runs = api.calls.indexOf("/v1/projects/demo/runs");
    expect(api.calls.indexOf("/v1/projects/demo/plugins/sample")).toBeLessThan(runs);
    expect(api.named[runs]).toBe("sample");
    send(frame.contentWindow, { bridge: BRIDGE, id: 2, method: "plans.list" });
    await refused(posted, 2, "not_granted");
    expect(api.calls.some((path) => path.includes("/plans"))).toBe(false);
  });

  it("goes by the grant of now: a plugin disabled, narrowed, turned off or removed while its page is open", async () => {
    const { frame, posted, client } = setup();
    send(frame.contentWindow, { bridge: BRIDGE, id: 1, method: "runs.list" });
    await waitFor(() => expect(posted).toHaveBeenCalledWith(expect.objectContaining({ id: 1, result: expect.anything() }), "*"));
    send(frame.contentWindow, { bridge: BRIDGE, id: 2, method: "runs.list" });
    await waitFor(() => expect(posted).toHaveBeenCalledWith(expect.objectContaining({ id: 2, result: expect.anything() }), "*"));
    // a read of the last GRANT_FRESH_MS stands: one read of the grant for both calls
    expect(api.calls.filter((path) => path === "/v1/projects/demo/plugins/sample")).toHaveLength(1);

    const cases = [
      [3, { ...PLUGIN, enabled: false, granted: [] }], // disabled in the project
      [4, { ...PLUGIN, granted: [] }], // upgraded to a version that asks for less
      [5, { ...PLUGIN, state: "disabled" }], // turned off by the hub
      [6, null], // removed from the hub
    ] as const;
    for (const [id, plugin] of cases) {
      // what the web holds still grants runs:read, read longer ago than GRANT_FRESH_MS
      client.setQueryData(pluginKeys.projectOne("demo", "sample"), PLUGIN, { updatedAt: Date.now() - GRANT_FRESH_MS - 1 });
      api.plugin = plugin;
      api.calls = [];
      send(frame.contentWindow, { bridge: BRIDGE, id, method: "runs.list" });
      await refused(posted, id, "not_granted");
      expect(api.calls).toEqual(["/v1/projects/demo/plugins/sample"]); // read again, and the runs never asked for
    }
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
