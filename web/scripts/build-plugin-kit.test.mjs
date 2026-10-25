import { readFileSync } from "node:fs";

import { describe, expect, it } from "vitest";

import { buildPluginKitCss, GLOBALS_CSS, KIT_CSS, readTokens } from "./build-plugin-kit.mjs";

const globals = readFileSync(GLOBALS_CSS, "utf8");

describe("the plugin kit's evo-ui.css", () => {
  it("is what pnpm build:plugin-kit makes of globals.css now, so a token changed without a rebuild fails", () => {
    expect(readFileSync(KIT_CSS, "utf8")).toBe(buildPluginKitCss(globals));
  });

  it("carries the light tokens of :root, the dark ones of .dark under html.dark, and the fonts and radii", () => {
    const { light, dark, theme } = readTokens(globals);
    expect(light).toContainEqual(["--brand", "#2f4bd8"]);
    expect(light).toContainEqual(["--background", "var(--canvas)"]);
    expect(dark).toContainEqual(["--canvas", "#09090b"]);
    expect(theme).toContainEqual(["--radius-sm", "6px"]);
    expect(theme.find(([name]) => name === "--font-sans")?.[1]).toMatch(/^"IBM Plex Sans", ui-sans-serif, system-ui/);
    expect(theme.some(([name]) => name.startsWith("--color-") || name.startsWith("--animate-"))).toBe(false);

    const css = buildPluginKitCss(globals);
    const darkBlock = css.slice(css.indexOf("html.dark {"), css.indexOf("}", css.indexOf("html.dark {")));
    expect(darkBlock).toContain("--canvas: #09090b;");
    expect(darkBlock).toContain("color-scheme: dark;");
    expect(css).not.toContain("--font-plex");
    expect(css).not.toMatch(/@font-face|url\(/);
    for (const name of ["evo-btn-primary", "evo-btn-destructive", "evo-card", "evo-table-wrap", "evo-badge", "evo-tabs"]) {
      expect(css).toContain(`.${name}`);
    }
  });

  it("follows a token changed in globals.css", () => {
    const changed = globals.replace("--brand: #2f4bd8;", "--brand: #123456;");
    expect(buildPluginKitCss(changed)).toContain("--brand: #123456;");
    expect(buildPluginKitCss(changed)).not.toBe(buildPluginKitCss(globals));
  });
});
