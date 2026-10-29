import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import {
  CLAUDE_PLUGIN_COMMANDS,
  CODEX_PLUGIN_COMMANDS,
  harnessInitCommand,
  hubLoginCommand,
  INSTALL_COMMAND,
  REGISTER_COMMAND,
  SERVICE_COMMAND,
} from "./commands";

// The Get started list may only name commands the CLI has: each one is checked against the getting-started guide the
// README links to, and docs/hub.md.
const repoDir = fileURLToPath(new URL("../../../..", import.meta.url));
const guide = readFileSync(path.join(repoDir, "docs/getting-started.md"), "utf8");
const hubDoc = readFileSync(path.join(repoDir, "docs/hub.md"), "utf8");
const pyproject = readFileSync(path.join(repoDir, "pyproject.toml"), "utf8");

describe("the Get started commands", () => {
  it("install the CLI with extras the package has", () => {
    expect(INSTALL_COMMAND).toBe("uv tool install 'evo-ak[graphify,worker]'");
    expect(guide).toContain(INSTALL_COMMAND);
    expect(pyproject).toMatch(/^graphify = \[/m);
    expect(pyproject).toMatch(/^worker = \[/m);
  });

  it("are the getting-started guide's own lines", () => {
    for (const command of [...CLAUDE_PLUGIN_COMMANDS, ...CODEX_PLUGIN_COMMANDS, SERVICE_COMMAND, REGISTER_COMMAND]) {
      expect(guide).toContain(command);
    }
    expect(guide).toContain(hubLoginCommand("https://hub.example.org"));
    expect(guide).toContain(harnessInitCommand(""));
    expect(hubDoc).toContain("evo-agents harness init");
    expect(guide).toContain(harnessInitCommand("acme"));
  });

  it("name the hub they sign in to", () => {
    expect(hubLoginCommand("https://hub.acme.dev")).toBe("evo-agents hub login --url https://hub.acme.dev");
  });

  it("give harness init a project name only when the hub would accept it", () => {
    expect(harnessInitCommand(" acme-tools ")).toBe("evo-agents harness init --project acme-tools");
    expect(harnessInitCommand("")).toBe("evo-agents harness init");
    expect(harnessInitCommand("Acme Tools")).toBe("evo-agents harness init");
    expect(harnessInitCommand("acme; rm -rf ~")).toBe("evo-agents harness init");
  });
});
