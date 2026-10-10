import { execFileSync } from "node:child_process";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { gzipSync } from "node:zlib";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { checkBudgets, checkRatchet, formatTable, main, measureRoutes, parseBudgets, toKb } from "./bundle-budget.mjs";

/** `length` characters that gzip poorly, the same on every run, so the fake chunks weigh a few kB each. */
function noise(seed, length) {
  let state = seed;
  let text = "";
  while (text.length < length) {
    state = (state * 1103515245 + 12345) % 2 ** 31;
    text += state.toString(36);
  }
  return text.slice(0, length);
}

const CHUNKS = {
  "framework.js": noise(1, 30_000),
  "main.js": noise(2, 12_000),
  "home.js": noise(3, 6_000),
  "run.js": noise(4, 20_000),
  "home.css": "body { color: red }\n",
};

const gz = (name) => gzipSync(Buffer.from(CHUNKS[name]), { level: 9 }).length;
const chunk = (name) => `.next/static/chunks/${name}`;

let webDir;

/** A fake build: chunk files and the route-bundle-stats.json `next build` writes, routes / and /runs/[id]. */
function writeBuild(dir, routes = { "/": ["home.js", "home.css"], "/runs/[id]": ["run.js"] }) {
  mkdirSync(path.join(dir, ".next", "static", "chunks"), { recursive: true });
  mkdirSync(path.join(dir, ".next", "diagnostics"), { recursive: true });
  for (const [name, text] of Object.entries(CHUNKS)) writeFileSync(path.join(dir, chunk(name)), text);
  const shared = ["framework.js", "main.js"];
  const rows = Object.entries(routes).map(([route, own]) => ({
    route,
    firstLoadUncompressedJsBytes: 0,
    firstLoadChunkPaths: [...own, ...shared].map(chunk),
  }));
  writeFileSync(path.join(dir, ".next", "diagnostics", "route-bundle-stats.json"), JSON.stringify(rows));
}

function writeBudgetFile(dir, routes) {
  writeFileSync(path.join(dir, "bundle-budget.json"), JSON.stringify({ routes }, null, 2));
}

function git(dir, ...args) {
  execFileSync("git", ["-c", "user.name=test", "-c", "user.email=test@example.com", ...args], { cwd: dir, stdio: "ignore" });
}

/** Commits the current bundle-budget.json (or none) in a fresh repository at `dir` and tags it `base`. */
function commitBase(dir) {
  git(dir, "init", "-q");
  git(dir, "add", "-A", "--", ".", ":!.next");
  git(dir, "commit", "-q", "--allow-empty", "-m", "base");
  git(dir, "tag", "base");
}

function run(argv) {
  const out = [];
  const err = [];
  const code = main(argv, { webDir, out: (line) => out.push(line), err: (line) => err.push(line) });
  return { code, out: out.join("\n"), err: err.join("\n") };
}

const homeBytes = () => gz("home.js") + gz("framework.js") + gz("main.js");
const runBytes = () => gz("run.js") + gz("framework.js") + gz("main.js");

beforeEach(() => {
  webDir = mkdtempSync(path.join(tmpdir(), "bundle-budget-"));
});

afterEach(() => {
  rmSync(webDir, { recursive: true, force: true });
});

describe("measureRoutes", () => {
  it("adds up the gzip size of each JS chunk listed for a route, shared chunks included, CSS left out", () => {
    writeBuild(webDir);
    const measured = measureRoutes(webDir);
    expect(measured.map((row) => row.route)).toEqual(["/runs/[id]", "/"]);
    const home = measured.find((row) => row.route === "/");
    expect(home.bytes).toBe(homeBytes());
    expect(home.chunks.map((c) => c.file)).not.toContain(chunk("home.css"));
    expect(home.chunks[0].bytes).toBeGreaterThanOrEqual(home.chunks[home.chunks.length - 1].bytes);
  });

  it("says to build first when the build left no stats", () => {
    expect(() => measureRoutes(webDir)).toThrow(/pnpm build/);
  });
});

describe("toKb", () => {
  it("rounds up to a tenth of a kB, so a budget set from a measure holds it", () => {
    expect(toKb(292_401)).toBe(292.5);
    expect(toKb(292_400)).toBe(292.4);
  });
});

describe("parseBudgets", () => {
  it("rejects a file without routes or with an entry lacking kB", () => {
    expect(() => parseBudgets("{}", "f")).toThrow(/routes/);
    expect(() => parseBudgets('{"routes": {"/": {}}}', "f")).toThrow(/kB/);
    expect(() => parseBudgets("{", "f")).toThrow(/JSON/);
  });
});

describe("checkBudgets", () => {
  it("names the route, its size, its budget and how to see its chunks when it is over", () => {
    writeBuild(webDir);
    const measured = measureRoutes(webDir);
    const kb = toKb(runBytes());
    const problems = checkBudgets(measured, { "/": { kB: 999 }, "/runs/[id]": { kB: kb - 0.3 } });
    expect(problems).toHaveLength(1);
    expect(problems[0]).toContain("/runs/[id]");
    expect(problems[0]).toContain(`${kb.toFixed(1)} kB`);
    expect(problems[0]).toContain(`${(kb - 0.3).toFixed(1)} kB`);
    expect(problems[0]).toContain("over its budget");
    expect(problems[0]).toContain("--chunks '/runs/[id]'");
  });

  it("passes a route at exactly its budget", () => {
    writeBuild(webDir);
    const measured = measureRoutes(webDir);
    expect(checkBudgets(measured, { "/": { kB: toKb(homeBytes()) }, "/runs/[id]": { kB: toKb(runBytes()) } })).toEqual([]);
  });

  it("fails a route without a budget and a budget without a route", () => {
    writeBuild(webDir);
    const problems = checkBudgets(measureRoutes(webDir), { "/": { kB: 999 }, "/gone": { kB: 1 } });
    expect(problems.some((p) => p.startsWith("/runs/[id] has no budget"))).toBe(true);
    expect(problems.some((p) => p.includes("lists /gone"))).toBe(true);
  });
});

describe("checkRatchet", () => {
  const base = { "/": { kB: 100 }, "/runs/[id]": { kB: 200, reason: "the trace" } };

  it("lets a budget go down or stay", () => {
    expect(checkRatchet({ "/": { kB: 90 }, "/runs/[id]": { kB: 200, reason: "the trace" } }, base, "main")).toEqual([]);
  });

  it("fails a raise without a reason, or with the reason the base already had", () => {
    expect(checkRatchet({ "/": { kB: 101 }, "/runs/[id]": { kB: 200 } }, base, "main")[0]).toMatch(
      /raises \/ from 100\.0 kB on main to 101\.0 kB without a new "reason"/,
    );
    expect(checkRatchet({ "/": { kB: 100 }, "/runs/[id]": { kB: 210, reason: "the trace" } }, base, "main")).toHaveLength(1);
  });

  it("accepts a raise or a new route that says why", () => {
    const budgets = {
      "/": { kB: 120, reason: "markdown preview on Home" },
      "/runs/[id]": { kB: 200, reason: "the trace" },
      "/new": { kB: 50, reason: "the new page" },
    };
    expect(checkRatchet(budgets, base, "main")).toEqual([]);
  });

  it("fails a new route without a reason", () => {
    expect(checkRatchet({ ...base, "/new": { kB: 50 } }, base, "main")[0]).toMatch(/adds \/new .* without a "reason"/);
  });
});

describe("formatTable", () => {
  it("prints each route with its size, budget and headroom", () => {
    writeBuild(webDir);
    const table = formatTable(measureRoutes(webDir), { "/": { kB: 999 } });
    expect(table.split("\n")[0]).toMatch(/^Route\s+First Load JS\s+Budget\s+Headroom$/);
    expect(table).toMatch(/\/runs\/\[id\]\s+[\d.]+ kB\s+none/);
  });
});

describe("main", () => {
  it("exits 0 when every route is within its budget and the base has no budget file", () => {
    writeBuild(webDir);
    commitBase(webDir);
    writeBudgetFile(webDir, { "/": { kB: toKb(homeBytes()) }, "/runs/[id]": { kB: toKb(runBytes()) } });
    const result = run(["--base", "base"]);
    expect(result.err).toBe("");
    expect(result.out).toContain("base has no bundle-budget.json");
    expect(result.code).toBe(0);
  });

  it("exits 1 and names the route when one is over its budget", () => {
    writeBuild(webDir);
    writeBudgetFile(webDir, { "/": { kB: toKb(homeBytes()) - 1 }, "/runs/[id]": { kB: toKb(runBytes()) } });
    commitBase(webDir);
    const result = run(["--base", "base"]);
    expect(result.code).toBe(1);
    expect(result.err).toMatch(/bundle-budget: \/ loads [\d.]+ kB of JS first, over its budget/);
  });

  it("exits 1 when the file raises a budget over the base ref's without a reason", () => {
    writeBuild(webDir);
    writeBudgetFile(webDir, { "/": { kB: toKb(homeBytes()) }, "/runs/[id]": { kB: toKb(runBytes()) } });
    commitBase(webDir);
    writeBudgetFile(webDir, { "/": { kB: toKb(homeBytes()) + 5 }, "/runs/[id]": { kB: toKb(runBytes()) } });
    expect(run(["--base", "base"]).code).toBe(1);
    writeBudgetFile(webDir, {
      "/": { kB: toKb(homeBytes()) + 5, reason: "room for the markdown preview" },
      "/runs/[id]": { kB: toKb(runBytes()) },
    });
    expect(run(["--base", "base"]).code).toBe(0);
  });

  it("fails when the base ref passed cannot be read", () => {
    writeBuild(webDir);
    commitBase(webDir);
    writeBudgetFile(webDir, { "/": { kB: 999 }, "/runs/[id]": { kB: 999 } });
    const result = run(["--base", "no-such-ref"]);
    expect(result.code).toBe(2);
    expect(result.err).toContain("cannot read the base ref no-such-ref");
  });

  it("lists a route's chunks largest first", () => {
    writeBuild(webDir);
    const result = run(["--chunks", "/runs/[id]"]);
    expect(result.code).toBe(0);
    const lines = result.out.split("\n");
    expect(lines[0]).toMatch(/^\/runs\/\[id\]: [\d.]+ kB of First Load JS \(gzip\) in 3 chunks$/);
    expect(lines.slice(1, 4).map((line) => line.trim().split(/\s+/).pop())).toEqual(
      [chunk("framework.js"), chunk("run.js"), chunk("main.js")],
    );
    expect(lines[4]).toContain("next experimental-analyze");
  });

  it("writes budgets equal to the measure, keeping reasons", () => {
    writeBuild(webDir);
    writeBudgetFile(webDir, { "/": { kB: 1, reason: "kept" } });
    expect(run(["--write"]).code).toBe(0);
    commitBase(webDir);
    const result = run(["--base", "base"]);
    expect(result.code).toBe(0);
    expect(result.out).toContain("2 routes within their budgets");
  });
});
