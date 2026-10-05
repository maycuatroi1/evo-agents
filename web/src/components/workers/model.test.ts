import { describe, expect, it } from "vitest";

import {
  filtersSearch,
  filterWorkers,
  joinCommand,
  matchesWorker,
  parseLabels,
  readCheckouts,
  readFilters,
  readRuntimes,
  registerCommand,
  summarize,
  workerView,
} from "./model";
import { parseWorkerId, type Worker } from "./queries";

function worker(overrides: Partial<Worker> = {}): Worker {
  return {
    id: 1,
    name: "binh-mbp",
    owner: "octo",
    status: "online",
    hostname: "binh-mbp.local",
    os: "macOS 15",
    arch: "arm64",
    agent_version: "0.3.0",
    slots: 2,
    labels: ["macos"],
    projects: ["evo-agents"],
    runtimes: {},
    checkouts: {},
    allow_web_terminal: false,
    held_runs: 0,
    created_at: "2026-10-05T07:00:00Z",
    last_heartbeat_at: "2026-10-05T07:10:00Z",
    drained_at: null,
    revoked_at: null,
    ...overrides,
  };
}

describe("workerView", () => {
  it("shows an online worker as busy while it holds a run and idle otherwise", () => {
    expect(workerView(worker())).toBe("idle");
    expect(workerView(worker({ held_runs: 1 }))).toBe("busy");
  });

  it("keeps the hub's offline, draining and revoked as they are, held runs or not", () => {
    expect(workerView(worker({ status: "offline", held_runs: 2 }))).toBe("offline");
    expect(workerView(worker({ status: "draining", held_runs: 1 }))).toBe("draining");
    expect(workerView(worker({ status: "revoked" }))).toBe("revoked");
  });
});

describe("filters", () => {
  const workers = [
    worker({ id: 1, name: "binh-mbp", held_runs: 1 }),
    worker({ id: 2, name: "lab-ws-01", labels: ["linux", "gpu"], owner: "lab", projects: ["evo-lms"] }),
    worker({ id: 3, name: "old-imac", status: "offline" }),
    worker({ id: 4, name: "gone", status: "revoked" }),
  ];

  it("reads only known statuses from the URL, and trims the search", () => {
    const params = (text: string) => new URLSearchParams(text);
    expect(readFilters(params("status=busy&q=%20gpu%20"))).toEqual({ status: "busy", q: "gpu" });
    expect(readFilters(params("status=online"))).toEqual({ status: null, q: "" });
    expect(filtersSearch({ status: "idle", q: "a b" })).toBe("?status=idle&q=a+b");
    expect(filtersSearch({ status: null, q: "" })).toBe("");
  });

  it("leaves revoked workers out of All, and finds them under Revoked", () => {
    expect(filterWorkers(workers, { status: null, q: "" }).map((w) => w.id)).toEqual([1, 2, 3]);
    expect(filterWorkers(workers, { status: "revoked", q: "" }).map((w) => w.id)).toEqual([4]);
    expect(filterWorkers(workers, { status: "busy", q: "" }).map((w) => w.id)).toEqual([1]);
  });

  it("matches every word against name, host, owner, labels and projects, ignoring case", () => {
    expect(matchesWorker(workers[1], "GPU lab")).toBe(true);
    expect(matchesWorker(workers[1], "evo-lms")).toBe(true);
    expect(matchesWorker(workers[1], "gpu windows")).toBe(false);
    expect(filterWorkers(workers, { status: null, q: "imac" }).map((w) => w.id)).toEqual([3]);
  });
});

describe("summarize", () => {
  it("counts each status, the runs held and the free slots of online workers", () => {
    const summary = summarize([
      worker({ held_runs: 1, slots: 2 }),
      worker({ id: 2, slots: 1 }),
      worker({ id: 3, status: "draining", slots: 4 }),
      worker({ id: 4, status: "offline", last_heartbeat_at: "2026-10-05T06:00:00Z" }),
      worker({ id: 5, status: "offline", last_heartbeat_at: "2026-10-05T05:00:00Z" }),
      worker({ id: 6, status: "offline", last_heartbeat_at: null }),
      worker({ id: 7, status: "revoked" }),
    ]);
    expect(summary).toMatchObject({ idle: 1, busy: 1, draining: 1, offline: 3, revoked: 1, live: 6 });
    expect(summary).toMatchObject({ heldRuns: 1, freeSlots: 2, totalSlots: 3, neverSeen: 1 });
    expect(summary.oldestOffline).toBe("2026-10-05T05:00:00Z");
  });
});

describe("what a heartbeat reports", () => {
  it("reads runtimes as flags, versions or objects, known ones first", () => {
    const runtimes = readRuntimes({
      zed: true,
      codex: { available: false, reason: "not found on PATH" },
      opencode: "1.18.34",
      "claude-code": { version: "2.1.289", auth: "signed in" },
      gemini: null,
    });
    expect(runtimes.map((r) => [r.name, r.available, r.version, r.detail])).toEqual([
      ["Claude Code", true, "2.1.289", "signed in"],
      ["opencode", true, "1.18.34", null],
      ["Codex CLI", false, null, "not found on PATH"],
      ["gemini", false, null, null],
      ["zed", true, null, null],
    ]);
  });

  it("treats a runtime that reports an error as missing, and cuts long text", () => {
    const [broken] = readRuntimes({ codex: { error: "x".repeat(500) } });
    expect(broken.available).toBe(false);
    expect(broken.detail?.length).toBe(201);
  });

  it("reads checkouts as paths or objects, by name", () => {
    expect(
      readCheckouts({
        "evo-agents": { path: "~/github/evo-agents", branch: "main" },
        api: "~/work/api",
        web: ["~/a", "~/b"],
        odd: 42,
      }),
    ).toEqual([
      { name: "api", path: "~/work/api", branch: null, detail: null },
      { name: "evo-agents", path: "~/github/evo-agents", branch: "main", detail: null },
      { name: "odd", path: "42", branch: null, detail: null },
      { name: "web", path: "~/a, ~/b", branch: null, detail: null },
    ]);
  });
});

describe("the registration form", () => {
  it("splits labels on commas and spaces, keeps the valid ones and names the rest", () => {
    expect(parseLabels(" macos, gpu  gpu,,-bad x/y ")).toEqual({ labels: ["macos", "gpu"], invalid: ["-bad", "x/y"], tooMany: false });
    expect(parseLabels(Array.from({ length: 17 }, (_, i) => `l${i}`).join(",")).tooMany).toBe(true);
  });

  it("builds the commands from what was typed, with the hostname when no valid name was", () => {
    expect(joinCommand("https://hub.example.org", "K7QM-4XPD")).toBe(
      "evo-agents worker join --url https://hub.example.org --code K7QM-4XPD",
    );
    expect(registerCommand({ name: "lab-ws-01", projects: ["a", "b"], slots: 2, labels: ["gpu"] })).toBe(
      "evo-agents worker register --name lab-ws-01 --project a --project b --slots 2 --label gpu",
    );
    expect(registerCommand({ name: "", projects: ["a"], slots: 1, labels: [] })).toBe(
      'evo-agents worker register --name "$(hostname -s)" --project a --slots 1',
    );
    expect(registerCommand({ name: "bad name; rm -rf", projects: [], slots: 1, labels: [] })).toContain('"$(hostname -s)"');
  });

  it("accepts only positive ids the API can hold", () => {
    expect(parseWorkerId("12")).toBe(12);
    expect(parseWorkerId("0")).toBeNull();
    expect(parseWorkerId("012")).toBeNull();
    expect(parseWorkerId("1e3")).toBeNull();
    expect(parseWorkerId("99999999999999999999")).toBeNull();
  });
});
