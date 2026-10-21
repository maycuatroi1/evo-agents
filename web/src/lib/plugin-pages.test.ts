import { describe, expect, it } from "vitest";

import { menuPages, pendingPermissions, pluginOn, shownPage } from "./plugin-pages";
import type { PluginPage, ProjectPlugin } from "./plugin-queries";

function page(name: string, extra: Partial<PluginPage> = {}): PluginPage {
  return { name, title: name, description: "", icon: "puzzle", menu: true, native: false, url: `/v1/plugin-ui/p/demo/1.a/${name}.html`, ...extra };
}

function plugin(name: string, extra: Partial<ProjectPlugin> = {}): ProjectPlugin {
  return {
    name,
    title: name,
    description: "",
    tier: "isolated",
    publisher: "acme",
    version: "1.0.0",
    state: "installed",
    disabled_reason: null,
    enabled: true,
    permissions: [],
    granted: [],
    pending: [],
    config_schema: null,
    pages: [page("board")],
    enabled_by: null,
    enabled_at: null,
    granted_by: null,
    granted_at: null,
    ...extra,
  };
}

describe("plugin pages", () => {
  it("lists in the menu the pages of plugins on in the project, framed ones only once the hub signed their URL", () => {
    const plugins = [
      plugin("a", { pages: [page("one"), page("quiet", { menu: false }), page("unsigned", { url: null })] }),
      plugin("core", { tier: "core", pages: [page("docs", { native: true, url: null })] }),
      plugin("off", { enabled: false }),
      plugin("broken", { state: "disabled" }),
    ];
    expect(menuPages(plugins).map(({ plugin: owner, page: item }) => `${owner.name}/${item.name}`)).toEqual(["a/one", "core/docs"]);
    expect(pluginOn(plugins[2])).toBe(false);
    expect(pluginOn(plugins[3])).toBe(false);
  });

  it("finds a page the project shows, and none for a plugin that is off or a page it lacks", () => {
    expect(shownPage(plugin("a"), "board")?.name).toBe("board");
    expect(shownPage(plugin("a"), "missing")).toBeNull();
    expect(shownPage(plugin("a", { enabled: false }), "board")).toBeNull();
    expect(shownPage(undefined, "board")).toBeNull();
    expect(shownPage(plugin("a", { pages: [page("board", { url: null })] }), "board")).toBeNull();
  });

  it("says what an upgrade asks for that the project has not granted, only once enabled", () => {
    expect(pendingPermissions(plugin("a", { pending: ["plans:read"] }))).toEqual(["plans:read"]);
    expect(pendingPermissions(plugin("a", { enabled: false, pending: ["plans:read"] }))).toEqual([]);
  });
});
