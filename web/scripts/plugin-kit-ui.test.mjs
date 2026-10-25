// @vitest-environment jsdom
import { readFileSync } from "node:fs";
import path from "node:path";

import { beforeEach, describe, expect, it } from "vitest";

import { KIT_DIR } from "./build-plugin-kit.mjs";

const SOURCE = readFileSync(path.join(KIT_DIR, "evo-ui.js"), "utf8");

/**
 * evo-ui.js run as in a frame: a window whose parent records what the page posts, and the one message listener the
 * kit adds, which a test calls as the hub's web would answer. jsdom loads no script, so a test fires the load of
 * each i18n/<locale>.js the kit inserts once it has called evo.messages as that file would.
 */
function framed() {
  const posted = [];
  const parent = { postMessage: (message) => posted.push(message) };
  let listener = null;
  const page = {
    parent,
    addEventListener: (type, handler) => {
      if (type === "message") listener = handler;
    },
  };
  new Function("window", "document", SOURCE)(page, document);
  const deliver = (data) => listener({ source: parent, data });
  return { evo: page.evo, posted, deliver };
}

function loadScripts(words) {
  for (const script of document.querySelectorAll("script[src^='i18n/']")) {
    const locale = script.getAttribute("src").slice("i18n/".length, -".js".length);
    script.dispatchEvent(new Event(words[locale] ? "load" : "error"));
  }
}

const CONTEXT = {
  bridge: "evo-plugin/1",
  project: "northwind",
  plugin: "lumen",
  page: "board",
  viewer: { login: "octo", role: "reader" },
  locale: "vi",
  theme: "dark",
  methods: ["context", "resize", "route", "navigate", "toast"],
  location: { path: "", query: {} },
  kit: 1,
};

beforeEach(() => {
  document.documentElement.className = "";
  document.documentElement.removeAttribute("lang");
  document.head.innerHTML = "";
});

describe("evo-ui.js", () => {
  it("reads the context, sets the theme and the language, loads the words and falls back to English", async () => {
    const { evo, posted, deliver } = framed();
    expect(posted[0]).toMatchObject({ bridge: "evo-plugin/1", method: "context", params: {} });
    deliver({ bridge: "evo-plugin/1", id: posted[0].id, result: CONTEXT });
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(document.documentElement.classList.contains("dark")).toBe(true);
    expect(document.documentElement.getAttribute("lang")).toBe("vi");
    const sources = [...document.querySelectorAll("script")].map((script) => script.getAttribute("src"));
    expect(sources).toEqual(["i18n/en.js", "i18n/vi.js"]);

    evo.messages("en", { title: "Board of {project}", only: "Only in English" });
    evo.messages("vi", { title: "Bảng của {project}" });
    loadScripts({ en: true, vi: true });
    expect(await evo.ready).toEqual(CONTEXT);
    expect(evo.context).toEqual(CONTEXT);
    expect(evo.t("title", { project: "northwind" })).toBe("Bảng của northwind");
    expect(evo.t("only")).toBe("Only in English");
    expect(evo.t("missing.key")).toBe("missing.key");

    deliver({ bridge: "evo-plugin/1", event: "theme", theme: "light" });
    expect(document.documentElement.classList.contains("dark")).toBe(false);
    expect(evo.context.theme).toBe("light");
  });

  it("wraps the bridge's answers and errors, and navigates by a sub path of the page", async () => {
    const { evo, posted, deliver } = framed();
    deliver({ bridge: "evo-plugin/1", id: posted[0].id, result: { ...CONTEXT, locale: "en", theme: "light" } });
    await new Promise((resolve) => setTimeout(resolve, 0));
    loadScripts({});
    await evo.ready;
    expect(document.documentElement.getAttribute("lang")).toBe("en");

    const toast = evo.toast("Saved");
    expect(posted.at(-1)).toMatchObject({ method: "toast", params: { text: "Saved", tone: "info" } });
    deliver({ bridge: "evo-plugin/1", id: posted.at(-1).id, error: { code: "invalid_params", message: "no" } });
    await expect(toast).rejects.toMatchObject({ code: "invalid_params", message: "no" });

    const navigated = evo.navigate("items/3");
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(posted.at(-1)).toMatchObject({ method: "navigate", params: { to: "/p/northwind/x/lumen/board/items/3" } });
    deliver({ bridge: "evo-plugin/1", id: posted.at(-1).id, result: { to: "/p/northwind/x/lumen/board/items/3" } });
    expect(await navigated).toEqual({ to: "/p/northwind/x/lumen/board/items/3" });

    const route = evo.route("items", { query: { q: "lumen" } });
    expect(posted.at(-1)).toMatchObject({ method: "route", params: { path: "items", query: { q: "lumen" } } });
    deliver({ bridge: "evo-plugin/1", id: posted.at(-1).id, result: { status: 200, body: [] } });
    expect(await route).toEqual({ status: 200, body: [] });
  });
});
