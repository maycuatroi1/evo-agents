import { describe, expect, it } from "vitest";

import type { Agent } from "@/lib/agents";

import { avatarProblem, createBody, draftOf, draftProblems, editBody, emptyDraft, fileBase64, hasProblems, versionChanges, withItem, withoutItem } from "./model";
import { optionsFrom } from "./options";

function agent(overrides: Partial<Agent> = {}): Agent {
  return {
    handle: "reviewer",
    project: "demo",
    name: "Reviewer",
    description: "Reads diffs.",
    instructions: "Be brief.\n",
    color: "#7c3aed",
    icon_url: null,
    avatar_sha256: null,
    is_default: false,
    permissions: { tool: { mode: "allowlist", allow: ["read"] } },
    restricted: ["tool"],
    version: 3,
    created_by: "octo",
    created_at: "2026-10-14T03:00:00Z",
    updated_at: "2026-10-14T03:00:00Z",
    deleted_at: null,
    ...overrides,
  };
}

describe("the agent form's checks", () => {
  it("refuses a blank or malformed handle, a project's name, a blank name and text over its length", () => {
    const draft = { ...emptyDraft(), handle: "Bad Handle", name: "  " };
    expect(draftProblems(draft, { creating: true, projects: ["demo"] })).toMatchObject({ handle: "pattern", name: "blank" });
    expect(draftProblems({ ...draft, handle: "demo", name: "Demo" }, { creating: true, projects: ["demo"] }).handle).toBe("project");
    expect(draftProblems({ ...draft, handle: "", name: "x".repeat(81) }, { creating: true, projects: [] })).toMatchObject({ handle: "blank", name: "long" });
    expect(draftProblems({ ...emptyDraft(), name: "Tab\there" }, { creating: false, projects: [] }).name).toBe("line");
    const long = { ...emptyDraft(), handle: "ok", name: "Ok", description: "d".repeat(1001), instructions: "i".repeat(8001) };
    expect(draftProblems(long, { creating: true, projects: [] })).toMatchObject({ description: "long", instructions: "long" });
    const fine = { ...emptyDraft(), handle: "ok-2", name: "Ok" };
    expect(hasProblems(draftProblems(fine, { creating: true, projects: ["demo"] }))).toBe(false);
  });

  it("checks each allowlist's size and the shape of what it names", () => {
    const draft = emptyDraft();
    draft.handle = "ok";
    draft.name = "Ok";
    draft.permissions.tool = { mode: "allowlist", allow: ["read", "shell"] };
    draft.permissions.skill = { mode: "allowlist", allow: Array.from({ length: 201 }, (_, i) => `s${i}`) };
    expect(draftProblems(draft, { creating: true, projects: [] }).axes).toEqual({ tool: "item", skill: "tooMany" });
  });

  it("adds an item as the hub keeps it, once, and removes it", () => {
    const rule = withItem({ mode: "allowlist", allow: [] }, "worker", " Mini ");
    expect(rule.allow).toEqual(["mini"]);
    expect(withItem(rule, "worker", "MINI")).toBe(rule);
    expect(withoutItem(rule, "mini").allow).toEqual([]);
  });
});

describe("what the form sends", () => {
  it("creates with every axis whole, the colour only when one was picked", () => {
    const draft = { ...emptyDraft(), handle: " reviewer ", name: " Reviewer ", description: " Reads diffs. " };
    draft.permissions.runtime = { mode: "allowlist", allow: ["claude-code"] };
    const body = createBody(draft);
    expect(body).toMatchObject({ handle: "reviewer", name: "Reviewer", description: "Reads diffs.", instructions: "" });
    expect(body).not.toHaveProperty("color");
    expect(body.permissions?.runtime).toEqual({ mode: "allowlist", allow: ["claude-code"] });
    expect(body.permissions?.tool).toEqual({ mode: "all" });
    expect(createBody({ ...draft, color: "#16a34a" }).color).toBe("#16a34a");
  });

  it("edits only what changed, against the version the form opened on", () => {
    const current = agent();
    expect(editBody(draftOf(current), current)).toBeNull(); // the stored trailing line break is no change
    const draft = draftOf(current);
    draft.name = "Code reviewer";
    draft.permissions.tool = { mode: "allowlist", allow: ["read", "bash"] };
    const body = editBody(draft, current);
    expect(body).toMatchObject({ name: "Code reviewer", if_version: 3 });
    expect(body?.permissions?.tool).toEqual({ mode: "allowlist", allow: ["read", "bash"] });
    expect(body).not.toHaveProperty("description");
  });

  it("never sends instructions or permissions for the project's default agent", () => {
    const current = agent({ handle: "demo", is_default: true, permissions: {}, restricted: [], instructions: "" });
    const draft = draftOf(current);
    draft.instructions = "Ignored";
    draft.permissions.tool = { mode: "allowlist", allow: ["read"] };
    expect(editBody(draft, current)).toBeNull();
    draft.description = "The demo project's own.";
    expect(editBody(draft, current)).toEqual({ description: "The demo project's own.", if_version: 3 });
  });

  it("takes a PNG, JPEG or WebP image up to 512 KiB, and sends its bytes in base64", async () => {
    expect(avatarProblem({ type: "image/gif", size: 10 })).toBe("type");
    expect(avatarProblem({ type: "image/png", size: 512 * 1024 + 1 })).toBe("size");
    expect(avatarProblem({ type: "image/webp", size: 512 * 1024 })).toBeNull();
    expect(await fileBase64(new Blob([new Uint8Array([0x89, 0x50, 0x4e, 0x47])]))).toBe("iVBORw==");
  });
});

describe("an agent's versions", () => {
  it("names what a version changed against the one before it", () => {
    const older = { name: "Reviewer", color: "#7c3aed", avatar_sha256: null, permissions: { tool: { mode: "all" } } };
    const newer = { name: "Code reviewer", color: "#7c3aed", avatar_sha256: "ab12", permissions: { tool: { mode: "allowlist", allow: ["read"] } } };
    expect(versionChanges(newer, older)).toEqual(["name", "avatar", "tool"]);
    expect(versionChanges(older, older)).toEqual([]);
  });
});

describe("the form's choices", () => {
  const worker = (name: string, labels: string[], status = "online", models: string[] = []) =>
    ({ name, owner: "octo", labels, status, runtimes: { "claude-code": { available: true, models }, codex: { available: false, models: ["gpt-5"] } } }) as never;

  it("lists secrets, skills, workers and their labels, models and the other projects as the hub answers them", () => {
    const options = optionsFrom(
      {
        secrets: [{ name: "npm-token", kind: "env", env_var: "NPM_TOKEN", url_prefix: null } as never],
        skills: [{ name: "team-notes", scope: "project", description: "Notes." } as never, { name: "create-exec-plan", scope: "global", description: "" } as never],
        workers: [worker("Desk", ["GPU"], "online", ["opus", "sonnet"]), worker("mini", ["gpu", "macos"], "offline", ["opus"]), worker("old", ["gpu"], "revoked")],
        projects: [{ name: "demo", role: "admin" } as never, { name: "docs", role: "writer" } as never],
      },
      "demo",
    );
    expect(options.failed).toEqual([]);
    expect(options.byAxis.credential.map((option) => option.value)).toEqual(["github-app", "npm-token"]);
    expect(options.byAxis.skill.map((option) => [option.value, option.tag])).toEqual([
      ["create-exec-plan", "global"],
      ["team-notes", "project"],
    ]);
    expect(options.byAxis.worker.map((option) => [option.value, option.detail])).toEqual([
      ["desk", "octo"],
      ["mini", "octo"],
      ["label:gpu", "2"],
      ["label:macos", "1"],
    ]);
    expect(options.byAxis.model).toEqual([
      { value: "opus", detail: "claude-code" },
      { value: "sonnet", detail: "claude-code" },
    ]);
    expect(options.byAxis.project.map((option) => option.value)).toEqual(["docs"]);
    expect(options.byAxis.tool.map((option) => option.value)).toEqual(["read", "edit", "bash", "web", "agent"]);
    expect(options.byAxis.runtime.map((option) => option.value)).toEqual(["claude-code", "opencode", "codex"]);
  });

  it("names the lists the hub did not answer, and still offers the App's token and the API's words", () => {
    const options = optionsFrom({ secrets: null, skills: null, workers: null, projects: null }, "demo");
    expect(options.failed).toEqual(["credential", "skill", "worker", "model", "project"]);
    expect(options.byAxis.credential.map((option) => option.value)).toEqual(["github-app"]);
    expect(options.byAxis.tool).toHaveLength(5);
  });
});
