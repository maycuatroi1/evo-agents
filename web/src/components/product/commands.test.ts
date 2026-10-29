import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { hubLoginCommand, INSTALL_COMMAND } from "./commands";

// The product page may only show commands the CLI has: each one is a line of README.md.
const repoDir = fileURLToPath(new URL("../../../..", import.meta.url));
const readme = readFileSync(path.join(repoDir, "README.md"), "utf8");
const pyproject = readFileSync(path.join(repoDir, "pyproject.toml"), "utf8");

describe("the product page's commands", () => {
  it("install the CLI with extras the package has", () => {
    expect(readme).toContain(`'evo-ak[graphify,worker]'`);
    expect(INSTALL_COMMAND).toBe("uv tool install 'evo-ak[graphify,worker]'");
    expect(pyproject).toMatch(/^graphify = \[/m);
    expect(pyproject).toMatch(/^worker = \[/m);
  });

  it("are the README's own lines", () => {
    expect(readme).toContain(INSTALL_COMMAND);
    expect(readme).toContain(hubLoginCommand("https://hub.example.org"));
  });

  it("sign in to the hub the page is served from", () => {
    expect(hubLoginCommand("https://hub.acme.dev")).toBe("evo-agents hub login --url https://hub.acme.dev");
  });
});
