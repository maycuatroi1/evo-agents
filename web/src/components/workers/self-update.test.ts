import { describe, expect, it } from "vitest";

import type { Worker } from "./queries";
import { commitUrl, LONG_WAIT_MS, shortCommit, type SelfUpdate, updateProblems, updateState, waitedMs } from "./self-update";

const SHA = "ca3e9af0f1b2c3d4e5f60718293a4b5c6d7e8f90";
const NIGHTLY = "def5678a1b2c3d4e5f60718293a4b5c6d7e8f901";
const NOW = Date.parse("2026-10-19T12:00:00Z");
const HOUR = 60 * 60 * 1000;

function report(overrides: Partial<SelfUpdate> = {}): SelfUpdate {
  return { state: "current", nightly_commit: SHA, checked_at: "2026-10-19T11:30:00Z", since: "2026-10-19T08:00:00Z", ...overrides };
}

function worker(overrides: Partial<Worker> = {}): Worker {
  return {
    id: 1,
    name: "binh-mbp",
    owner: "octo",
    status: "online",
    hostname: "binh-mbp.local",
    os: "macOS 15",
    arch: "arm64",
    agent_version: "0.22.0",
    agent_commit: SHA,
    slots: 2,
    labels: [],
    plugins: [],
    version_locked: false,
    self_update: report(),
    update_now: null,
    nightly: null,
    projects: ["evo-agents"],
    runtimes: {},
    checkouts: {},
    free_slots: null,
    allow_web_terminal: false,
    dispatch_from: "any",
    held_runs: 0,
    created_at: "2026-10-05T07:00:00Z",
    last_heartbeat_at: "2026-10-19T11:59:30Z",
    drained_at: null,
    revoked_at: null,
    ...overrides,
  };
}

describe("commitUrl", () => {
  it("links a full SHA-1 or SHA-256 commit to its page on GitHub", () => {
    expect(commitUrl(SHA)).toBe(`https://github.com/maycuatroi1/evo-agents/commit/${SHA}`);
    expect(commitUrl("a".repeat(64))).toBe(`https://github.com/maycuatroi1/evo-agents/commit/${"a".repeat(64)}`);
  });

  it("links nothing else", () => {
    for (const text of [null, undefined, "", "ca3e9af", SHA.toUpperCase(), `${SHA}/../x`, "a".repeat(41)]) {
      expect(commitUrl(text), String(text)).toBeNull();
    }
  });

  it("shortens a commit to 7 characters", () => {
    expect(shortCommit(SHA)).toBe("ca3e9af");
  });
});

describe("updateProblems", () => {
  it("says nothing while all is well, and nothing for a daemon that reports nothing", () => {
    expect(updateProblems(worker(), NOW)).toEqual([]);
    expect(updateProblems(worker({ self_update: null }), NOW)).toEqual([]);
    expect(updateProblems(worker({ self_update: report({ state: "updating", nightly_commit: NIGHTLY }) }), NOW)).toEqual([]);
  });

  it("names an update that failed, a daemon that cannot update itself, and a long wait, most serious first", () => {
    const failed = report({ state: "waiting", error: "uv tool install: exit 2", error_at: "2026-10-19T11:00:00Z", since: "2026-10-18T20:00:00Z" });
    expect(updateProblems(worker({ self_update: failed }), NOW)).toEqual(["failed", "waiting"]);
    expect(updateProblems(worker({ self_update: report({ state: "unsupported", reason: "editable install" }) }), NOW)).toEqual(["unsupported"]);
  });

  it("drops an error once a later check found the daemon on nightly", () => {
    const fixed = report({ error: "reading nightly: timed out", error_at: "2026-10-19T09:00:00Z", checked_at: "2026-10-19T11:30:00Z" });
    expect(updateProblems(worker({ self_update: fixed }), NOW)).toEqual([]);
    const still = report({ error: "reading nightly: timed out", error_at: "2026-10-19T11:45:00Z", checked_at: "2026-10-19T11:30:00Z" });
    expect(updateProblems(worker({ self_update: still }), NOW)).toEqual(["failed"]);
    expect(updateProblems(worker({ self_update: report({ error: "boom", error_at: null }) }), NOW)).toEqual(["failed"]);
  });

  it("counts a wait as long from 12 hours, and only while the worker sends heartbeats and the clock is known", () => {
    const waiting = (hours: number) => report({ state: "waiting", nightly_commit: NIGHTLY, since: new Date(NOW - hours * HOUR).toISOString() });
    expect(updateProblems(worker({ self_update: waiting(11.9) }), NOW)).toEqual([]);
    expect(updateProblems(worker({ self_update: waiting(12) }), NOW)).toEqual(["waiting"]);
    expect(updateProblems(worker({ self_update: waiting(30), status: "draining" }), NOW)).toEqual(["waiting"]);
    expect(updateProblems(worker({ self_update: waiting(30), status: "offline" }), NOW)).toEqual([]);
    expect(updateProblems(worker({ self_update: waiting(30) }), null)).toEqual([]);
    expect(updateProblems(worker({ self_update: { ...waiting(30), since: null } }), NOW)).toEqual([]);
  });

  it("says nothing for a locked or revoked worker", () => {
    const broken = report({ state: "unsupported", error: "boom" });
    expect(updateProblems(worker({ self_update: broken, version_locked: true }), NOW)).toEqual([]);
    expect(updateProblems(worker({ self_update: { ...broken, state: "locked" } }), NOW)).toEqual([]);
    expect(updateProblems(worker({ self_update: broken, status: "revoked" }), NOW)).toEqual([]);
  });
});

describe("waitedMs", () => {
  it("measures a wait from when the hub first got it", () => {
    expect(waitedMs(report({ state: "waiting", since: new Date(NOW - 3 * HOUR).toISOString() }), NOW)).toBe(3 * HOUR);
    expect(waitedMs(report({ state: "current" }), NOW)).toBeNull();
    expect(waitedMs(report({ state: "waiting", since: "not a date" }), NOW)).toBeNull();
  });

  it("is LONG_WAIT_MS at half a day", () => {
    expect(LONG_WAIT_MS).toBe(12 * HOUR);
  });
});

describe("updateState", () => {
  it("puts the owner's lock first, then what the daemon says", () => {
    expect(updateState(worker({ version_locked: true, self_update: report({ state: "waiting" }) }))).toBe("locked");
    expect(updateState(worker())).toBe("current");
    expect(updateState(worker({ self_update: report({ state: "waiting" }) }))).toBe("waiting");
    expect(updateState(worker({ self_update: report({ state: "updating" }) }))).toBe("updating");
    expect(updateState(worker({ self_update: report({ state: "unsupported" }) }))).toBe("unsupported");
  });

  it("knows an unlock the daemon has not taken yet, a daemon that says nothing, and a state it does not know", () => {
    expect(updateState(worker({ self_update: report({ state: "locked" }) }))).toBe("unlocking");
    expect(updateState(worker({ self_update: null }))).toBe("unreported");
    expect(updateState(worker({ self_update: null, last_heartbeat_at: null }))).toBe("none");
    expect(updateState(worker({ self_update: report({ state: "rolling_back" }) }))).toBe("other");
  });
});
