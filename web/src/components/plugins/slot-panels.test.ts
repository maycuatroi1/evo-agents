import { describe, expect, it } from "vitest";

import type { PluginPage, ProjectPlugin } from "@/lib/plugin-queries";

import { slotPages } from "./slot-panels";

function page(name: string, extra: Partial<PluginPage> = {}): PluginPage {
  return { name, title: name, description: "", icon: "puzzle", menu: false, native: false, url: `/v1/plugin-ui/x/demo/1/${name}.html`, slot: null, ...extra };
}

function plugin(name: string, pages: PluginPage[], extra: Partial<ProjectPlugin> = {}): ProjectPlugin {
  return { name, title: name, enabled: true, state: "installed", pages, ...extra } as unknown as ProjectPlugin;
}

describe("slotPages", () => {
  it("lists each enabled plugin's framed page of the slot, in the hub's order", () => {
    const plugins = [
      plugin("northwind", [page("overview", { menu: true }), page("decide", { slot: "decide" })]),
      plugin("lumen", [page("decide", { slot: "decide" })]),
    ];
    expect(slotPages(plugins, "decide").map(({ plugin: item, page: shown }) => `${item.name}/${shown.name}`)).toEqual([
      "northwind/decide",
      "lumen/decide",
    ]);
  });

  it("leaves out a plugin that is off and a page not signed yet, and finds nothing where no page has the slot", () => {
    const plugins = [
      plugin("northwind", [page("decide", { slot: "decide" })], { enabled: false }),
      plugin("lumen", [page("decide", { slot: "decide" })], { state: "disabled" }),
      plugin("northwind-board", [page("decide", { slot: "decide", url: null })]),
      plugin("lumen-notes", [page("overview", { menu: true })]),
    ];
    expect(slotPages(plugins, "decide")).toEqual([]);
  });
});
