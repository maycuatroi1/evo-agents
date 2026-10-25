import { describe, expect, it } from "vitest";

import { evidenceLink } from "./evidence-links";

const decision = { project: "north wind", run_id: 42 };

describe("a decision's evidence as links", () => {
  it("links a run, an event of a run and a file of the decision's run to their pages in the project", () => {
    expect(evidenceLink("run:7", decision)).toMatchObject({ href: "/p/north%20wind/runs/7", words: "run", values: { id: "7" }, external: false });
    expect(evidenceLink("run:7:31", decision)).toMatchObject({ href: "/p/north%20wind/runs/7", words: "runEvent", values: { id: "7", seq: "31" } });
    expect(evidenceLink("output:9", decision)).toMatchObject({ href: "/p/north%20wind/runs/42?view=outputs", words: "output", values: { id: "9", run: 42 } });
  });

  it("links a problem's evidence to the problem, a memory to its page and a file of a repo to the graph's search", () => {
    expect(evidenceLink("P-3/E-2", decision)).toMatchObject({ href: "/p/north%20wind/problems/P-3", words: "problem", values: { problem: "P-3", evidence: "E-2" } });
    expect(evidenceLink("memory:12:4", decision)).toMatchObject({ href: "/p/north%20wind/memories/12", words: "memory", values: { id: "12", revision: "4" } });
    expect(evidenceLink("code:web:src/proxy.ts", decision)).toMatchObject({ href: "/p/north%20wind/kg?q=src%2Fproxy.ts", words: "code", mono: true, values: { repo: "web", path: "src/proxy.ts" } });
    expect(evidenceLink("code:web:src/proxy.ts:58", decision)).toMatchObject({ words: "codeLine", values: { repo: "web", path: "src/proxy.ts", line: "58" } });
  });

  it("opens an https page in a new tab under a short label, and only an https one", () => {
    const page = evidenceLink("https://github.com/maycuatroi1/evo-agents/pull/250", decision);
    expect(page).toMatchObject({ href: "https://github.com/maycuatroi1/evo-agents/pull/250", external: true, words: "url", values: { page: "github.com/maycuatroi1/evo-agents/pull/250" } });
    const long = evidenceLink(`https://example.com/${"a".repeat(120)}`, decision);
    expect(String(long.values.page)).toHaveLength(64);
    expect(String(long.values.page).endsWith("...")).toBe(true);
    expect(evidenceLink("http://example.com/x", decision)).toMatchObject({ href: null, words: "other" });
    expect(evidenceLink("javascript:alert(1)", decision)).toMatchObject({ href: null, words: "other" });
  });

  it("names a session or a line of its transcript without a link, as nothing else shows them", () => {
    expect(evidenceLink("session:3f2a9c1e-0000-4000-8000-0000000000aa", decision)).toMatchObject({ href: null, words: "session", values: { id: "3f2a9c1e" } });
    expect(evidenceLink("session:3f2a9c1e:corrections:2", decision)).toMatchObject({ href: null, words: "session" });
    expect(evidenceLink("transcript:3f2a9c1e:120", decision)).toMatchObject({ href: null, words: "transcript", values: { id: "3f2a9c1e", line: "120" } });
    expect(evidenceLink("transcript:3f2a9c1e:subagents/a:7", decision)).toMatchObject({ words: "transcript", values: { line: "7" } });
  });
});
