import { describe, expect, it } from "vitest";

import {
  clampSize,
  closeKind,
  FRAME_INPUT,
  FRAME_RESIZE,
  helloMessage,
  inputFrames,
  MAX_FRAME_BYTES,
  outputPayload,
  resizeFrame,
  SESSION_MAX_AGE_MS,
  sessionHours,
  sessionTooOld,
  terminalAccess,
  terminalUrl,
} from "./terminal-model";

const run = { state: "interactive" as const, dispatched_by: "binh", worker_id: 4 };
const worker = { id: 4, owner: "binh", allow_web_terminal: true };

describe("terminalAccess", () => {
  it("offers the terminal to the run's owner on a worker of theirs that allows it", () => {
    expect(terminalAccess(run, { login: "binh" }, worker)).toEqual({ open: true });
    expect(terminalAccess({ ...run, state: "leased" }, { login: "binh" }, worker)).toEqual({ open: true });
    expect(terminalAccess({ ...run, state: "running" }, { login: "binh" }, worker)).toEqual({ open: true });
  });

  it("keeps it for the owner, closed, while the run is in a state no terminal opens in", () => {
    for (const state of ["queued", "verifying", "review", "done", "failed", "lost", "cancelled"] as const) {
      expect(terminalAccess({ ...run, state }, { login: "binh" }, worker)).toEqual({ open: false });
    }
  });

  it("hides it from anyone else, and when the worker is not the owner's or does not allow it", () => {
    expect(terminalAccess(run, null, worker)).toBeNull();
    expect(terminalAccess(run, { login: "an" }, worker)).toBeNull();
    expect(terminalAccess(run, { login: "binh" }, undefined)).toBeNull();
    expect(terminalAccess(run, { login: "binh" }, { ...worker, owner: "an" })).toBeNull();
    expect(terminalAccess(run, { login: "binh" }, { ...worker, allow_web_terminal: false })).toBeNull();
    expect(terminalAccess(run, { login: "binh" }, { ...worker, id: 5 })).toBeNull();
    expect(terminalAccess({ ...run, worker_id: null }, { login: "binh" }, worker)).toBeNull();
  });
});

describe("the 12-hour limit", () => {
  const created = "2026-10-05T00:00:00Z";
  const at = (ms: number) => Date.parse(created) + ms;

  it("is reached at 12 hours, and unknown before the clock is read", () => {
    expect(sessionTooOld(created, at(SESSION_MAX_AGE_MS - 1))).toBe(false);
    expect(sessionTooOld(created, at(SESSION_MAX_AGE_MS))).toBe(true);
    expect(sessionTooOld(created, null)).toBe(false);
    expect(sessionTooOld(null, at(SESSION_MAX_AGE_MS * 2))).toBe(false);
    expect(sessionTooOld("not a date", at(0))).toBe(false);
  });

  it("counts whole hours", () => {
    expect(sessionHours(created, at(13.9 * 3_600_000))).toBe(13);
    expect(sessionHours(created, at(-1))).toBe(0);
  });
});

describe("the wire", () => {
  it("opens the websocket on the page's own origin, wss under https", () => {
    expect(terminalUrl({ protocol: "http:", host: "localhost:3324" }, "demo", 12)).toBe("ws://localhost:3324/v1/projects/demo/runs/12/terminal");
    expect(terminalUrl({ protocol: "https:", host: "hub.example.org" }, "a b", 3)).toBe("wss://hub.example.org/v1/projects/a%20b/runs/3/terminal");
  });

  it("says hello with the CSRF value and a size the hub accepts", () => {
    expect(JSON.parse(helloMessage("token", 120, 40))).toEqual({ csrf: "token", cols: 120, rows: 40 });
    expect(clampSize(0, 5000)).toEqual({ cols: 1, rows: 1000 });
    expect(clampSize(80.7, Number.NaN)).toEqual({ cols: 80, rows: 1 });
  });

  it("sends input as UTF-8 in frames of at most 64 KiB, type byte included", () => {
    const [frame] = inputFrames("chào");
    expect(frame[0]).toBe(FRAME_INPUT);
    expect(new TextDecoder().decode(frame.subarray(1))).toBe("chào");

    const paste = new Uint8Array(MAX_FRAME_BYTES * 2).fill(97);
    const frames = inputFrames(paste);
    expect(frames.map((part) => part.length)).toEqual([MAX_FRAME_BYTES, MAX_FRAME_BYTES, 3]);
    expect(frames.every((part) => part[0] === FRAME_INPUT)).toBe(true);
    expect(inputFrames("")).toEqual([]);
  });

  it("writes a resize as type 2 and two big-endian uint16", () => {
    expect([...resizeFrame(300, 40)]).toEqual([FRAME_RESIZE, 1, 44, 0, 40]);
    expect([...resizeFrame(5000, 0)]).toEqual([FRAME_RESIZE, 3, 232, 0, 1]);
  });

  it("reads only the worker's output frames", () => {
    const output = new Uint8Array([1, 104, 105]).buffer;
    expect([...(outputPayload(output) ?? [])]).toEqual([104, 105]);
    expect(outputPayload(new Uint8Array([0, 104]).buffer)).toBeNull();
    expect(outputPayload(new ArrayBuffer(0))).toBeNull();
    expect(outputPayload("text")).toBeNull();
  });
});

describe("closeKind", () => {
  it("names each code of the hub, and what the page did itself", () => {
    expect(closeKind(1000)).toBe("ended");
    expect(closeKind(1000, { closedByUser: true })).toBe("closed");
    expect(closeKind(1006, { closedByUser: true })).toBe("closed");
    expect(closeKind(1001)).toBe("shutdown");
    expect(closeKind(1003)).toBe("protocol");
    expect(closeKind(1009)).toBe("protocol");
    expect(closeKind(1006)).toBe("lost");
    expect(closeKind(1011)).toBe("unavailable");
    expect(closeKind(4401)).toBe("signIn");
    expect(closeKind(4403)).toBe("forbidden");
    expect(closeKind(4403, { sessionOld: true })).toBe("signIn");
    expect(closeKind(4408)).toBe("timeout");
    expect(closeKind(4409)).toBe("busy");
    expect(closeKind(4426)).toBe("upgrade");
  });
});
