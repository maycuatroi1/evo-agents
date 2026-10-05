import { describe, expect, it } from "vitest";

import type { RunMove } from "./log-model";
import type { Run } from "./queries";
import {
  leaseSecondsLeft,
  readDiffstat,
  readUsage,
  readVerify,
  runControls,
  type RunViewer,
  shortSha,
  stepperModel,
  utf8Bytes,
} from "./run-model";

const base = {
  state: "running",
  approval: "review",
  queued_at: "2026-10-05T07:00:00Z",
  leased_at: "2026-10-05T07:00:05Z",
  started_at: "2026-10-05T07:00:09Z",
  finished_at: null,
  dispatched_by: "octo",
  cancel_requested_at: null,
  takeover_requested_at: null,
  handback_requested_at: null,
  lease_expires_at: "2026-10-05T07:05:09Z",
} satisfies Partial<Run>;

const run = (extra: Partial<Run> = {}) => ({ ...base, ...extra }) as Run;
const move = (seq: number, from: Run["state"] | null, to: Run["state"], at = `2026-10-05T07:0${seq}:00Z`): RunMove => ({
  seq,
  at,
  from,
  to,
  actor: "worker",
  reason: null,
});

const owner: RunViewer = { login: "octo", role: "writer", admin: false };
const ownerReader: RunViewer = { login: "octo", role: "reader", admin: false };
const other: RunViewer = { login: "mona", role: "admin", admin: true };

describe("stepperModel", () => {
  it("marks the states passed, the current one and the ones to come, with review only for a run held for it", () => {
    const stepper = stepperModel(run(), []);
    expect(stepper.items.map((item) => `${item.phase}:${item.status}`)).toEqual([
      "queued:done",
      "leased:done",
      "running:current",
      "verifying:todo",
      "review:todo",
      "done:todo",
    ]);
    expect(stepper.items.map((item) => item.at)).toEqual([base.queued_at, base.leased_at, base.started_at, null, null, null]);
    expect(stepperModel(run({ approval: "auto" }), []).items.map((item) => item.phase)).not.toContain("review");
  });

  it("puts an interactive run on Running, and takes the times of later states from the log", () => {
    expect(stepperModel(run({ state: "interactive" }), []).items[2]).toMatchObject({ phase: "running", status: "current" });
    const moves = [move(1, "running", "verifying"), move(2, "verifying", "review")];
    const stepper = stepperModel(run({ state: "review" }), moves);
    expect(stepper.items.find((item) => item.phase === "verifying")).toMatchObject({ status: "done", at: moves[0].at });
    expect(stepper.items.find((item) => item.phase === "review")).toMatchObject({ status: "current", at: moves[1].at });
  });

  it("completes every state of a done run", () => {
    const stepper = stepperModel(run({ state: "done", approval: "auto", finished_at: "2026-10-05T07:09:00Z" }), []);
    expect(stepper.items.every((item) => item.status === "done")).toBe(true);
    expect(stepper.items.at(-1)?.at).toBe("2026-10-05T07:09:00Z");
    expect(stepper.ended).toBeNull();
  });

  it("stops a failed run at the state its final move left", () => {
    const stepper = stepperModel(run({ state: "failed" }), [move(1, "running", "verifying"), move(2, "verifying", "failed")]);
    expect(stepper.ended).toBe("failed");
    expect(stepper.items.map((item) => item.status)).toEqual(["done", "done", "done", "stopped", "todo", "todo"]);
  });

  it("falls back to the run's timestamps when the log has no final move", () => {
    expect(stepperModel(run({ state: "cancelled", leased_at: null, started_at: null }), []).items[0].status).toBe("stopped");
    expect(stepperModel(run({ state: "lost", started_at: null }), []).items[1].status).toBe("stopped");
    expect(stepperModel(run({ state: "cancelled" }), [move(3, "review", "cancelled")]).items[4].status).toBe("stopped");
  });
});

describe("runControls", () => {
  it("gives nothing to anyone but the owner, a hub admin included", () => {
    expect(runControls(run(), other)).toMatchObject({ owner: false, cancel: "none", takeover: "none", message: false });
    expect(runControls(run(), null).owner).toBe(false);
  });

  it("offers a running run's owner cancel, takeover and messages", () => {
    expect(runControls(run(), owner)).toEqual({
      owner: true,
      cancel: "offer",
      takeover: "offer",
      handback: "none",
      approve: false,
      rerun: false,
      message: true,
    });
  });

  it("shows an open ask as asked, and stops messages once a cancel is asked", () => {
    expect(runControls(run({ takeover_requested_at: "2026-10-05T07:01:00Z" }), owner).takeover).toBe("asked");
    const cancelling = runControls(run({ cancel_requested_at: "2026-10-05T07:01:00Z" }), owner);
    expect(cancelling).toMatchObject({ cancel: "asked", message: false });
    expect(runControls(run({ state: "interactive" }), owner)).toMatchObject({ takeover: "none", handback: "offer" });
    expect(runControls(run({ state: "interactive", handback_requested_at: "2026-10-05T07:02:00Z" }), owner).handback).toBe("asked");
  });

  it("needs the writer role to approve a run in review or rerun one that ended", () => {
    expect(runControls(run({ state: "review" }), owner)).toMatchObject({ approve: true, cancel: "offer", message: false });
    expect(runControls(run({ state: "review" }), ownerReader).approve).toBe(false);
    expect(runControls(run({ state: "failed" }), owner)).toMatchObject({ rerun: true, cancel: "none", takeover: "none" });
    expect(runControls(run({ state: "done" }), ownerReader).rerun).toBe(false);
    expect(runControls(run({ state: "queued" }), owner)).toMatchObject({ cancel: "offer", takeover: "none", message: true });
  });
});

describe("facts", () => {
  it("counts UTF-8 bytes, as the API's message limit does", () => {
    expect(utf8Bytes("abc")).toBe(3);
    expect(utf8Bytes("Tiếng Việt")).toBe(14);
  });

  it("counts the lease down only while a worker holds the run", () => {
    const now = Date.parse("2026-10-05T07:04:09Z");
    expect(leaseSecondsLeft(run(), now)).toBe(60);
    expect(leaseSecondsLeft(run(), Date.parse("2026-10-05T07:06:00Z"))).toBe(0);
    expect(leaseSecondsLeft(run({ state: "review" }), now)).toBeNull();
    expect(leaseSecondsLeft(run(), null)).toBeNull();
  });

  it("reads what the worker reported, leaving out what is not in shape", () => {
    expect(readDiffstat({ files: 3, insertions: 236, deletions: 2 })).toEqual({ files: 3, insertions: 236, deletions: 2 });
    expect(readDiffstat({ files: 3 })).toBeNull();
    expect(readDiffstat(null)).toBeNull();
    expect(readVerify([{ command: "pnpm test", exit_code: 0, duration_ms: 900 }, { exit_code: 1 }, "x"])).toEqual([
      { command: "pnpm test", exitCode: 0, durationMs: 900 },
    ]);
    expect(readUsage({ input_tokens: 10, output: { tokens: 2 }, model: "x" })).toEqual([
      { key: "input_tokens", value: 10 },
      { key: "output.tokens", value: 2 },
    ]);
    expect(shortSha("7c1e9a2f00ddeeff")).toBe("7c1e9a2");
  });
});
