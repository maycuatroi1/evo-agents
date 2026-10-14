import { describe, expect, it } from "vitest";

import { type Agent, agentModels, agentProblems, agentWorkers, allowed, allows, allowsWorker, profileField, usableAgents } from "./agents";
import { agentInitial, inkOn, normalizedItem, PALETTE, validItem } from "./agent-rules";

function agent(overrides: Partial<Agent> = {}): Agent {
  return {
    handle: "reviewer",
    project: "demo",
    name: "Reviewer",
    description: "",
    instructions: "",
    color: "#2563eb",
    icon_url: null,
    avatar_sha256: null,
    is_default: false,
    permissions: {},
    restricted: [],
    version: 1,
    created_by: "octo",
    created_at: "2026-10-14T03:00:00Z",
    updated_at: "2026-10-14T03:00:00Z",
    deleted_at: null,
    ...overrides,
  };
}

describe("agent permissions", () => {
  it("reads an axis left out, or of mode all, as allowing every value", () => {
    expect(allowed({}, "tool")).toBeNull();
    expect(allowed({ tool: { mode: "all" } }, "tool")).toBeNull();
    expect(allowed({ tool: { mode: "allowlist", allow: ["read"] } }, "tool")).toEqual(["read"]);
    expect(allowed({ tool: { mode: "allowlist" } }, "tool")).toEqual([]);
  });

  it("compares skills and workers ignoring case, the other axes as written", () => {
    const permissions = { skill: { mode: "allowlist" as const, allow: ["Team-Notes"] }, model: { mode: "allowlist" as const, allow: ["opus"] } };
    expect(allows(permissions, "skill", "team-notes")).toBe(true);
    expect(allows(permissions, "model", "Opus")).toBe(false);
    expect(allows(permissions, "model", "opus")).toBe(true);
    expect(allows(permissions, "runtime", "codex")).toBe(true);
  });

  it("lets a worker claim by its name or one of its labels", () => {
    const permissions = { worker: { mode: "allowlist" as const, allow: ["mini", "label:gpu"] } };
    expect(allowsWorker(permissions, "MINI", [])).toBe(true);
    expect(allowsWorker(permissions, "desk", ["GPU"])).toBe(true);
    expect(allowsWorker(permissions, "desk", ["macos"])).toBe(false);
    expect(allowsWorker({}, "desk", [])).toBe(true);
  });

  it("checks an allowlist item by the shape its axis takes", () => {
    expect(validItem("credential", "github-app")).toBe(true);
    expect(validItem("credential", "GitHub")).toBe(false);
    expect(validItem("tool", "bash")).toBe(true);
    expect(validItem("tool", "mcp:linear")).toBe(true);
    expect(validItem("tool", "shell")).toBe(false);
    expect(validItem("worker", "label:gpu")).toBe(true);
    expect(validItem("worker", "label:")).toBe(false);
    expect(validItem("project", "Demo")).toBe(false);
    expect(validItem("model", "anthropic/claude-sonnet-4-5")).toBe(true);
    expect(validItem("model", "two\nlines")).toBe(false);
    expect(validItem("runtime", "codex")).toBe(true);
    expect(validItem("runtime", "gemini")).toBe(false);
    expect(normalizedItem("worker", "  Mini ")).toBe("mini");
    expect(normalizedItem("skill", " Team-Notes ")).toBe("Team-Notes");
  });
});

describe("agents in a list and a picker", () => {
  it("keeps the agents a run can go under, the default first", () => {
    const list = [agent({ handle: "zeta" }), agent({ handle: "gone", deleted_at: "2026-10-14T04:00:00Z" }), agent({ handle: "demo", is_default: true }), agent({ handle: "alpha" })];
    expect(usableAgents(list).map((each) => each.handle)).toEqual(["demo", "alpha", "zeta"]);
  });

  it("draws the first letter or digit of the name, else of the handle", () => {
    expect(agentInitial({ name: "  ánh sáng", handle: "light" })).toBe("Á");
    expect(agentInitial({ name: "(42) bot", handle: "bot" })).toBe("4");
    expect(agentInitial({ name: "--", handle: "dash" })).toBe("D");
  });

  it("writes the initial in white or ink, whichever stands out more on the colour", () => {
    expect(inkOn("#2563eb")).toBe("#ffffff");
    expect(inkOn("#d97706")).toBe("#0e1116");
    expect(inkOn("#16a34a")).toBe("#0e1116");
    expect(inkOn("not a colour")).toBe("#ffffff");
    for (const colour of PALETTE) expect(["#ffffff", "#0e1116"]).toContain(inkOn(colour));
  });

  it("names the agent in a dispatch body only when it is not the project's default", () => {
    expect(profileField(null)).toEqual({});
    expect(profileField(agent({ handle: "demo", is_default: true }))).toEqual({});
    expect(profileField(agent())).toEqual({ profile: "reviewer" });
  });
});

describe("what an agent refuses of a dispatch", () => {
  const workers = [
    { name: "desk", labels: ["gpu"] },
    { name: "mini", labels: [] },
  ];

  it("narrows the visitor's workers to those the agent lets claim its runs", () => {
    const limited = agent({ permissions: { worker: { mode: "allowlist", allow: ["label:gpu"] } } });
    expect(agentWorkers(workers, limited).map((worker) => worker.name)).toEqual(["desk"]);
    expect(agentWorkers(workers, null)).toHaveLength(2);
  });

  it("says which runtime, model, workers and skill the agent leaves out", () => {
    const limited = agent({
      permissions: {
        runtime: { mode: "allowlist", allow: ["claude-code"] },
        model: { mode: "allowlist", allow: ["opus"] },
        worker: { mode: "allowlist", allow: ["laptop"] },
        skill: { mode: "allowlist", allow: ["team-notes"] },
      },
    });
    expect(agentProblems(limited, { runtime: "codex", model: "sonnet", workers, skill: "create-exec-plan" })).toEqual([
      { kind: "runtime", runtime: "codex", allow: ["claude-code"] },
      { kind: "model", model: "sonnet", allow: ["opus"] },
      { kind: "workers", allow: ["laptop"] },
      { kind: "skill", skill: "create-exec-plan" },
    ]);
    expect(agentProblems(limited, { runtime: "any", model: "opus", workers: [{ name: "laptop", labels: [] }], skill: "team-notes" })).toEqual([]);
  });

  it("refuses any runtime when the agent allows none, and a deleted agent always", () => {
    const none = agent({ permissions: { runtime: { mode: "allowlist", allow: [] } } });
    expect(agentProblems(none, { runtime: "any" })).toEqual([{ kind: "runtime", runtime: "any", allow: [] }]);
    expect(agentProblems(agent({ deleted_at: "2026-10-14T04:00:00Z" }), { runtime: "any" })).toEqual([{ kind: "deleted" }]);
    // Workers not read yet, or none at all, are the outlook's to explain.
    expect(agentProblems(agent({ permissions: { worker: { mode: "allowlist", allow: [] } } }), { runtime: "any", workers: [] })).toEqual([]);
  });

  it("offers the models the agent allows when it limits them, else what the workers list", () => {
    expect(agentModels(agent({ permissions: { model: { mode: "allowlist", allow: ["opus"] } } }), ["sonnet"])).toEqual(["opus"]);
    expect(agentModels(agent(), ["sonnet"])).toEqual(["sonnet"]);
    expect(agentModels(null, ["sonnet"])).toEqual(["sonnet"]);
  });
});
